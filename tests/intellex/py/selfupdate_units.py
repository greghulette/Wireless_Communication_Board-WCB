"""Intellex updating itself (src/selfupdate.py, Greg 2026-10-15: "an update intellex button"), under its venv, with
nothing reaching GitHub and nothing of Greg's replaced:

- _GitHub is a threaded HTTP server on 127.0.0.1 standing in for api.github.com: /repos/greghulette/Intellex/releases/
  latest and /releases/tags/v<v> answer what a case gives it (a 404 by default, as for a repo with no release), and
  /dl/<name> serves a release file. selfupdate.API points at it for the case.
- The running app is a stand-in too: sys.executable is pointed at a file (windows) or into an Intellex.app (macos) under
  the stage's scratch directory, selfupdate.mode is set for the case, and selfupdate.restart only records the call.
- Source mode works on throwaway git repositories: a bare 'origin', the checkout (selfupdate.root), and a second clone
  that pushes new commits.

INTELLEX_OFFLINE (the harness's leash) is lifted inside a case only, since every request goes to 127.0.0.1.

group "selfupdate" (intellex.selfupdate_unit).
"""
import contextlib
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import SkipCase, case, check  # noqa: E402

REPO = "/repos/greghulette/Intellex"


class _GitHub:
    """api.github.com for one repo's releases, and the release files: {tag: {"assets": {name: bytes}}}."""

    def __init__(self):
        self.releases, self.latest, self.asked = {}, None, []
        gh = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                gh.asked.append(path)
                body, code, ctype = b'{"message": "Not Found"}', 404, "application/json"
                if path == f"{REPO}/releases/latest" and gh.latest:
                    body, code = json.dumps(gh._rel(gh.latest)).encode(), 200
                elif path.startswith(f"{REPO}/releases/tags/"):
                    tag = path.rsplit("/", 1)[1]
                    if tag in gh.releases:
                        body, code = json.dumps(gh._rel(tag)).encode(), 200
                elif path.startswith("/dl/"):
                    name = path[4:]
                    for r in gh.releases.values():
                        if name in r["assets"]:
                            body, code, ctype = r["assets"][name], 200, "application/octet-stream"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _rel(self, tag):
        return {"tag_name": tag, "html_url": f"https://example.invalid/releases/{tag}",
                "assets": [{"name": n, "browser_download_url": f"{self.url}/dl/{n}", "size": len(b)}
                           for n, b in self.releases[tag]["assets"].items()]}

    def release(self, ver, files, sums=True, latest=True):
        assets = dict(files)
        if sums:
            assets["SHA256SUMS.txt"] = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n"
                                               for n, b in files.items()).encode()
        self.releases[f"v{ver}"] = {"assets": assets}
        if latest:
            self.latest = f"v{ver}"

    def close(self):
        self.srv.shutdown()


@contextlib.contextmanager
def _as(ctx, mode, exe=None):
    """selfupdate with a fresh state, pointed at a stand-in GitHub, running as `mode` (exe: sys.executable), online,
    with restart() recorded -> (selfupdate, github, restarts)."""
    import selfupdate
    gh = _GitHub()
    saved = (selfupdate.API, selfupdate.mode, selfupdate.restart, sys.executable, os.environ.get("INTELLEX_OFFLINE"))
    restarts = []
    selfupdate.API = gh.url
    selfupdate.mode = lambda: mode
    selfupdate.restart = lambda: restarts.append(time.time())
    if exe:
        sys.executable = str(exe)
    os.environ["INTELLEX_OFFLINE"] = ""
    _reset(selfupdate)
    try:
        yield selfupdate, gh, restarts
    finally:
        selfupdate.API, selfupdate.mode, selfupdate.restart, sys.executable, off = saved
        if off is None:
            os.environ.pop("INTELLEX_OFFLINE", None)
        else:
            os.environ["INTELLEX_OFFLINE"] = off
        _reset(selfupdate)
        gh.close()


def _reset(selfupdate):
    with selfupdate._lock:
        selfupdate._state.update(checked=False, checking=False, at=0.0, error="", reason="", latest="", available=False,
                                 notes_url="", asset="", behind=0, branch="",
                                 install={"running": False, "step": "", "percent": 0, "error": "", "done": False,
                                          "restarting": False})
    for f in (selfupdate._cache_file(), selfupdate._updated_file()):
        with contextlib.suppress(OSError):
            f.unlink()


def _wait_install(selfupdate, restarts, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        ins = selfupdate.status()["install"]
        if not ins["running"] and (ins["error"] or (ins["done"] and restarts)):
            return ins
        time.sleep(0.1)
    raise AssertionError(f"the install did not finish within {timeout:.0f} s: {selfupdate.status()['install']}")


def _scratch(ctx, name):
    d = os.path.join(ctx.stage, "scratch", "selfupdate", name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


NEXT = "9.9.9"          # newer than any VERSION the app will carry for a long while


# ------------------------------------------------------------------ version
@case("version.info() reads the build stamp only in a built app - a checkout that was once built keeps "
      "src/build_stamp.py, and run from source it claimed to be that old build; version.parse orders releases",
      group="selfupdate")
def version_info(ctx):
    import version
    stamp = os.path.join(ctx.src, "build_stamp.py")
    with open(stamp, "w", encoding="utf-8") as f:
        f.write('COMMIT = "hilstale"\nBUILT = "2026-09-09 14:21"\n')
    sys.modules.pop("build_stamp", None)
    try:
        i = version.info()
        check(i["commit"] != "hilstale" and i["built"] != "2026-09-09 14:21",
              f"run from source, info() took the stale build stamp: {i}")
        sys.frozen = True
        try:
            sys.modules.pop("build_stamp", None)
            i = version.info()
            check(i["commit"] == "hilstale", f"frozen, info() ignored the build stamp: {i}")
        finally:
            del sys.frozen
    finally:
        os.remove(stamp)
        sys.modules.pop("build_stamp", None)
    p = version.parse
    check(p("v0.2.0") == p("0.2.0") and p("0.2.0") < p("0.10.0") < p("1.0.0"), "release order")
    check(p("0.2.0-rc1") < p("0.2.0") and p("x") is None and p("0.2") is None, "pre-releases and non-versions")


# ------------------------------------------------------------------ the check
@case("the check against GitHub Releases: none published, up to date, a newer one with this computer's build and "
      "its SHA256SUMS.txt (offered), one without this computer's build or without checksums (not offered), offline "
      "(nothing asked), and a second check inside the hour answered from the cache", group="selfupdate")
def check_release(ctx):
    import version
    for mode, name in (("windows", f"Intellex-{NEXT}-windows.exe"), ("macos", None)):
        with _as(ctx, mode) as (su, gh, _):
            name = name or su.asset_name(NEXT, "macos")
            s = su.check(force=True)
            check(not s["available"] and s["reason"] == "no Intellex release has been published yet",
                  f"{mode}, no release: {s['available']} {s['reason']!r}")
            gh.release(version.VERSION, {su.asset_name(version.VERSION, mode): b"same"})
            s = su.check(force=True)
            check(not s["available"] and s["reason"] == "up to date", f"{mode}, same version: {s['reason']!r}")
            gh.release(NEXT, {name: b"new"})
            s = su.check(force=True)
            check(s["available"] and s["latest"] == NEXT and s["asset"] == name and not s["reason"],
                  f"{mode}, {NEXT} with {name}: available {s['available']}, asset {s['asset']!r}, {s['reason']!r}")
            gh.release(NEXT, {"Intellex-9.9.9-otheros.bin": b"x"})
            s = su.check(force=True)
            check(not s["available"] and "has no build for this computer" in s["reason"],
                  f"{mode}, no build for it: {s['reason']!r}")
            gh.release(NEXT, {name: b"new"}, sums=False)
            s = su.check(force=True)
            check(not s["available"] and "SHA256SUMS.txt" in s["reason"], f"{mode}, no checksums: {s['reason']!r}")
            # Inside the hour a check that is not forced is answered from the cache file: GitHub is not asked.
            gh.release(NEXT, {name: b"new"})
            su.check(force=True)
            n = len(gh.asked)
            _reset_state_only(su)
            s = su.check()
            check(s["available"] and len(gh.asked) == n, f"{mode}: an unforced check asked GitHub again "
                                                         f"({gh.asked[n:]})")
            os.environ["INTELLEX_OFFLINE"] = "1"
            n = len(gh.asked)
            s = su.check(force=True)
            check(not s["available"] and "offline" in s["reason"] and len(gh.asked) == n,
                  f"{mode}, offline: {s['reason']!r}, asked {gh.asked[n:]}")


def _reset_state_only(su):
    with su._lock:
        su._state.update(checked=False, at=0.0)


# ------------------------------------------------------------------ windows
@case("windows: the verified download takes the running Intellex.exe's name, the running one becomes "
      "Intellex.old.exe (renamed, as Windows allows for a running .exe), the app restarts, the next start deletes the "
      ".old; a download that does not match SHA256SUMS.txt changes nothing; a flash running refuses to start",
      group="selfupdate")
def install_windows(ctx):
    d = _scratch(ctx, "windows")
    exe = os.path.join(d, "Intellex.exe")
    with open(exe, "wb") as f:
        f.write(b"OLD")
    with _as(ctx, "windows", exe) as (su, gh, restarts):
        ok, why = su.start_install(lambda: "a flash is running")
        check(not ok and why == "a flash is running", f"a busy host started an update: {ok} {why!r}")
        new = b"NEW-" + os.urandom(64)
        gh.release(NEXT, {f"Intellex-{NEXT}-windows.exe": new})
        check(su.check(force=True)["available"], "the release was not offered")
        ok, why = su.start_install()
        check(ok, f"the install did not start: {why}")
        ins = _wait_install(su, restarts)
        check(not ins["error"] and ins["done"] and ins["restarting"], f"the install: {ins}")
        check(open(exe, "rb").read() == new, "Intellex.exe is not the download")
        old = os.path.join(d, "Intellex.old.exe")
        check(open(old, "rb").read() == b"OLD", "the running copy was not kept as Intellex.old.exe")
        check(len(restarts) == 1, f"restart() called {len(restarts)} time(s)")
        rec = json.loads(su._updated_file().read_text(encoding="utf-8"))
        check(rec.get("to") == NEXT, f"the update record says {rec}")
        su.cleanup_after_update()
        check(not os.path.exists(old), "the next start did not delete Intellex.old.exe")

    with open(exe, "wb") as f:
        f.write(b"OLD")
    with _as(ctx, "windows", exe) as (su, gh, restarts):
        name = f"Intellex-{NEXT}-windows.exe"
        gh.release(NEXT, {name: b"GOOD"})
        gh.releases[f"v{NEXT}"]["assets"][name] = b"TAMPERED"        # served bytes no longer match the checksum
        check(su.check(force=True)["available"], "the release was not offered")
        su.start_install()
        ins = _wait_install(su, restarts)
        check("does not match SHA256SUMS.txt" in ins["error"] and "nothing was changed" in ins["error"],
              f"a tampered download: {ins['error']!r}")
        check(open(exe, "rb").read() == b"OLD" and not os.path.exists(os.path.join(d, "Intellex.old.exe")),
              "a download that failed its checksum still replaced something")
        check(not restarts, "restarted after a failed update")


# ------------------------------------------------------------------ macos
def _fake_app(where, content):
    app = os.path.join(where, "Intellex.app")
    macos = os.path.join(app, "Contents", "MacOS")
    os.makedirs(macos)
    with open(os.path.join(macos, "Intellex"), "wb") as f:
        f.write(content)
    os.chmod(os.path.join(macos, "Intellex"), 0o755)
    os.makedirs(os.path.join(app, "Contents", "Frameworks", "Py.framework", "Versions", "A"))
    os.symlink("Versions/A", os.path.join(app, "Contents", "Frameworks", "Py.framework", "Current"))
    return app


@case("macos: the release zip (ditto, as release.yml makes it) unpacks into an Intellex.app that takes the running "
      "bundle's place with its symlinks kept, the running bundle goes to the user data dir as Intellex.prev.app, and "
      "the app restarts", group="selfupdate")
def install_macos(ctx):
    if sys.platform != "darwin" or not shutil.which("ditto"):
        raise SkipCase("needs macOS's ditto")
    d = _scratch(ctx, "macos")
    apps = os.path.join(d, "Applications")
    os.makedirs(apps)
    running = _fake_app(apps, b"OLD")
    built = os.path.join(d, "built")
    os.makedirs(built)
    new_bin = b"NEW-" + os.urandom(64)
    _fake_app(built, new_bin)
    z = os.path.join(d, "release.zip")
    subprocess.run(["ditto", "-c", "-k", "--keepParent", os.path.join(built, "Intellex.app"), z], check=True)
    with _as(ctx, "macos", os.path.join(running, "Contents", "MacOS", "Intellex")) as (su, gh, restarts):
        name = su.asset_name(NEXT, "macos")
        gh.release(NEXT, {name: open(z, "rb").read()})
        check(su.check(force=True)["available"], "the release was not offered")
        su.start_install()
        ins = _wait_install(su, restarts)
        check(not ins["error"] and ins["done"], f"the install: {ins}")
        check(open(os.path.join(running, "Contents", "MacOS", "Intellex"), "rb").read() == new_bin,
              "Intellex.app is not the download")
        link = os.path.join(running, "Contents", "Frameworks", "Py.framework", "Current")
        check(os.path.islink(link) and os.readlink(link) == "Versions/A", "the bundle's symlink did not survive")
        prev = su._updates_dir() / "Intellex.prev.app"
        check((prev / "Contents" / "MacOS" / "Intellex").read_bytes() == b"OLD",
              "the running bundle was not kept as Intellex.prev.app")
        check(len(restarts) == 1, f"restart() called {len(restarts)} time(s)")
        shutil.rmtree(prev, ignore_errors=True)


# ------------------------------------------------------------------ source
def _git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=hil", "-c", "user.email=hil@example.invalid", *args], cwd=cwd,
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def _commit(cwd, name, text):
    with open(os.path.join(cwd, name), "w", encoding="utf-8") as f:
        f.write(text)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", f"hil {name}")


@case("source: a checkout follows its branch's upstream - up to date, then 2 commits behind (offered), fast-forwarded "
      "and restarted; uncommitted changes refuse with nothing moved; a checkout with commits of its own refuses to "
      "merge", group="selfupdate")
def install_source(ctx):
    if not shutil.which("git"):
        raise SkipCase("no git")
    d = _scratch(ctx, "source")
    origin, work, other = (os.path.join(d, x) for x in ("origin.git", "work", "other"))
    _git(d, "init", "-q", "--bare", "-b", "main", origin)
    _git(d, "clone", "-q", origin, work)
    _git(work, "checkout", "-q", "-b", "main")
    _commit(work, "a.txt", "1")
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(d, "clone", "-q", origin, other)
    import selfupdate
    saved_root = selfupdate.root
    selfupdate.root = lambda: __import__("pathlib").Path(work)
    try:
        with _as(ctx, "source") as (su, gh, restarts):
            s = su.check(force=True)
            check(not s["available"] and s["reason"] == "up to date" and s["branch"] == "main",
                  f"a current checkout: {s['available']} {s['reason']!r} {s['branch']!r}")
            _commit(other, "b.txt", "2")
            _commit(other, "c.txt", "3")
            _git(other, "push", "-q")
            s = su.check(force=True)
            check(s["available"] and s["behind"] == 2, f"2 commits pushed: available {s['available']}, "
                                                        f"behind {s['behind']}, {s['reason']!r}")
            # Uncommitted changes: refused, HEAD where it was.
            head = _git(work, "rev-parse", "HEAD")
            with open(os.path.join(work, "a.txt"), "w", encoding="utf-8") as f:
                f.write("edited")
            su.start_install()
            ins = _wait_install(su, restarts)
            check("uncommitted changes" in ins["error"] and _git(work, "rev-parse", "HEAD") == head and not restarts,
                  f"a dirty checkout: {ins['error']!r}")
            _git(work, "checkout", "-q", "--", "a.txt")
            with su._lock:
                su._state["install"]["error"] = ""
            su.start_install()
            ins = _wait_install(su, restarts)
            check(not ins["error"] and ins["done"], f"the update: {ins}")
            check(_git(work, "rev-parse", "HEAD") == _git(other, "rev-parse", "HEAD"),
                  "the checkout was not fast-forwarded to its upstream")
            check(len(restarts) == 1, f"restart() called {len(restarts)} time(s)")
            # Commits of its own: cannot fast-forward, refused.
            _commit(work, "mine.txt", "local")
            _commit(other, "d.txt", "4")
            _git(other, "push", "-q")
            check(su.check(force=True)["available"], "the new upstream commit was not offered")
            head = _git(work, "rev-parse", "HEAD")
            su.start_install()
            ins = _wait_install(su, restarts)
            check("cannot fast-forward" in ins["error"] and _git(work, "rev-parse", "HEAD") == head,
                  f"a diverged checkout: {ins['error']!r}")
    finally:
        selfupdate.root = saved_root


# ------------------------------------------------------------------ the host
@case("the host's routes: GET /_api/app-update says what runs and what the check found; install with nothing to "
      "install, and while a flash is running, is 409 with the reason", group="selfupdate")
def host_routes(ctx):
    import host
    from bridge_units import _server
    s = _server()
    code, body = s.req("GET", "/_api/app-update")
    for k in ("current", "mode", "available", "checked", "install", "reason"):
        check(k in (body or {}), f"GET /_api/app-update lacks {k!r}: {sorted(body or {})}")
    code, body = s.req("POST", "/_api/app-update/install", {})
    check(code == 409 and "no update to install" in (body or {}).get("error", ""), f"nothing available: {code} {body}")
    import selfupdate
    with selfupdate._lock:
        selfupdate._state["available"] = True
    with host._flash_lock:
        host._flash_state["running"] = True
    try:
        code, body = s.req("POST", "/_api/app-update/install", {})
        check(code == 409 and "flash is running" in (body or {}).get("error", ""), f"while flashing: {code} {body}")
    finally:
        with host._flash_lock:
            host._flash_state["running"] = False
        _reset(selfupdate)


if __name__ == "__main__":
    sys.exit(_ixpy.main())
