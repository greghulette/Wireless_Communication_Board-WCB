"""/_api/flash-wcb and /_api/flash end to end inside one process, under Intellex's venv: Intellex's real build_app() on a
local port, a FakeTransport where the COM port would be, the fake esptool (tests/intellex/fixtures/fake_esptool) first on
PYTHONPATH, and a firmware cache seeded for the test branch hil-fake, offline (INTELLEX_OFFLINE). No port is opened and
no board is touched: the script checks, before any flash route is called, that `python -m esptool` answers with the
fake's signature, and every esptool argv names COMFAKE.

group "pipeline" (intellex.flash_pipeline_fake, IX-WP3): Intellex src/host.py:979-1109 - one flash at a time across both
tools (409), the port released before esptool and always handed back after (success or not), app-only / full / factory
write lists through wcb_flash, the NaviCore app-only path through flash.py, esptool's own words on a failure, and the
USB-only and INTELLEX_SERIAL_ALLOW refusals.

group "esptool5_should" (intellex.flash_esptool5_output, a (should) test, found writing this): Intellex runs esptool 5.3.1
(`esptool>=4.7`, requirements.txt:17), whose progress lines read `Writing at 0x00010000 [=====>   ]  25.0% ...`
(esptool logger.py:223-248). flash.py's _PCT_RE (src/flash.py:414, also used by wcb_flash.py:38-39, :390) matches only
esptool 4's `(25 %)`, so /_api/flash-status reports 0 % for the whole write and jumps to 100 at the end. And flash.py
still spells the NaviCore write esptool 4's way (write_flash, --flash_mode, default_reset: src/flash.py:434-438), which
esptool 5 answers with 'Deprecated:' warnings in the user's flash log - what wcb_flash.py:352-354 says it avoids.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import FIXTURES, case, check, seed_cache  # noqa: E402

BRANCH = "hil-fake"
WCB_TAG = "9.9.9_010000RJAN2026_hilfake"
NC_TAG = "9.9.9_010000RJAN2026"
SIGNATURE = "FAKE-ESPTOOL-FOR-INTELLEX-HIL"
_STATE = {}


def _blob(tag, n):
    return (tag.encode() * (n // len(tag) + 1))[:n]


def _setup(ctx):
    """Once per process: the fake esptool on PYTHONPATH (checked), the leash, the seeded cache and branch, the server."""
    if _STATE:
        return _STATE
    fake = os.path.join(FIXTURES, "fake_esptool")
    os.environ["PYTHONPATH"] = fake + os.pathsep + os.environ.get("PYTHONPATH", "")
    log = os.path.join(ctx.stage, "scratch", "esptool.jsonl")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    os.environ["FAKE_ESPTOOL_LOG"] = log
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE"          # the flash claim checks it too (host.py:1011-1014)
    os.environ["INTELLEX_OFFLINE"] = "1"
    sig = subprocess.run([sys.executable, "-m", "esptool", "--fake-signature"], capture_output=True, text=True,
                         timeout=60, cwd=ctx.stage, env=dict(os.environ))
    if sig.stdout.strip() != SIGNATURE:
        raise AssertionError("`python -m esptool` is not the fake here - refusing to call any flash route")
    images = {f"WCB_{WCB_TAG}_ESP32.bin": _blob("wcb-app", 3000), f"WCB_{WCB_TAG}_ESP32_part.bin": _blob("part", 3072),
              f"WCB_{WCB_TAG}_ESP32_boot.bin": _blob("boot", 2000)}
    seed_cache(ctx, "wcb", BRANCH, images)
    nc = {f"NaviCore_{NC_TAG}_ESP32S3.bin": _blob("nc-app", 4000)}
    seed_cache(ctx, "navicore", BRANCH, nc)
    settings = os.path.join(os.environ["LOCALAPPDATA"], "Intellex", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"branch": {"wcb": BRANCH, "navicore": BRANCH}}, f)
    _STATE.update(log=log, sha={n: hashlib.sha256(b).hexdigest() for n, b in {**images, **nc}.items()},
                  server=_Server())
    return _STATE


class _Server:
    """Intellex's build_app() on 127.0.0.1:<free>, in this process, with bridge.rebuild() handing out FakeTransports."""

    def __init__(self):
        import host
        from transport import Transport

        class FakeTransport(Transport):
            def __init__(self, spec, on_data, on_lost):
                super().__init__(on_data, on_lost)
                self.spec, self._open, self.writes = spec, False, []

            def open(self):
                self._open = True

            def write(self, data):
                self.writes.append(bytes(data))

            def close(self):
                self._open = False

            @property
            def is_open(self):
                return self._open

        self.host, self.Fake, self.made = host, FakeTransport, []
        b = host.bridge
        b.rebuild = lambda: self._make(dict(b._spec or {}))
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if not self.ready.wait(20):
            raise AssertionError("Intellex's app did not start in-process within 20 s")

    def _make(self, spec):
        t = self.Fake(spec, self.host.bridge._on_transport_data, self.host.bridge._on_transport_lost)
        self.made.append(t)
        return t

    def _run(self):
        from aiohttp import web
        asyncio.set_event_loop(self.loop)
        runner = web.AppRunner(self.host.build_app())
        self.loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    def req(self, method, path, body=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


def _esptool_runs(st, since):
    with open(st["log"], encoding="utf-8") as f:
        return [json.loads(x) for x in f.read().splitlines()[since:] if x.strip()]


def _log_len(st):
    if not os.path.exists(st["log"]):
        return 0
    with open(st["log"], encoding="utf-8") as f:
        return len(f.read().splitlines())


def _flash(st, route, body, poll=0.1, timeout=90):
    """POST a flash route, check the second claim is refused, and poll /_api/flash-status to the end -> (final status,
    the percents seen while it ran, the esptool runs it made)."""
    s = st["server"]
    since = _log_len(st)
    code, j = s.req("POST", route, body)
    check(code == 200 and j.get("ok") and j.get("started"), f"POST {route} {body}: {code} {j}")
    for other in ("/_api/flash", "/_api/flash-wcb"):
        code2, j2 = s.req("POST", other, {})
        check(code2 == 409 and "already running" in (j2 or {}).get("error", ""),
              f"a second flash ({other}) while one runs: {code2} {j2}")
    pcts, deadline = [], time.monotonic() + timeout
    while True:
        code, stt = s.req("GET", "/_api/flash-status")
        if not stt.get("running"):
            break
        pcts.append(stt.get("percent", 0))
        if time.monotonic() > deadline:
            raise AssertionError(f"the flash was still running after {timeout} s")
        time.sleep(poll)
    return stt, pcts, _esptool_runs(st, since)


def _write(runs):
    w = [r for r in runs if r["cmd"] in ("write-flash", "write_flash")]
    check(len(w) == 1, f"{len(w)} esptool writes, expected 1")
    return w[0]


def _arg(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


@case("an app-only WCB update: one claim, the port released, esptool asked, the cache written, the port handed back",
      group="pipeline")
def wcb_update(ctx):
    st = _setup(ctx)
    s = st["server"]
    code, j = s.req("POST", "/_api/attach", {"kind": "serial", "port": "COMFAKE"})
    check(code == 200 and j.get("ok"), f"attach COMFAKE: {code} {j}")
    made0 = len(s.made)
    stt, pcts, runs = _flash(st, "/_api/flash-wcb", {"appOnly": True})
    log = stt.get("log") or []
    check(stt.get("ok") is True and not stt.get("error"), f"the update failed: {stt.get('error', '')[:300]}")
    check(stt.get("version") == WCB_TAG, f"flash-status version {stt.get('version')!r}, expected {WCB_TAG!r}")
    check(stt.get("percent") == 100, f"percent {stt.get('percent')} at the end")
    check(pcts == sorted(pcts), f"the progress went backwards: {pcts}")
    for want in ("Releasing COMFAKE", "esptool --chip auto --port COMFAKE flash-id", "Chip: ESP32 -> ESP32 binary, 4 MB",
                 f"WARNING firmware source branch is '{BRANCH}'", "offline - using the cached firmware listing",
                 f"offline - using the cached {BRANCH} copy of WCB_{WCB_TAG}_ESP32.bin", "Update: app only",
                 "Hash of data verified.", "Reattached.", f"board is running {WCB_TAG}"):
        check(any(want in x for x in log), f"the flash log has no line with {want!r} ({len(log)} lines)")
    check([r["cmd"] for r in runs] == ["flash-id", "write-flash"], f"esptool runs: {[r['cmd'] for r in runs]}")
    fid = runs[0]["argv"]
    for a, v in (("--chip", "auto"), ("--port", "COMFAKE"), ("--before", "default-reset"), ("--after", "no-reset")):
        check(_arg(fid, a) == v, f"flash-id argv {a} = {_arg(fid, a)!r}, expected {v!r}")
    w = _write(runs)
    argv = w["argv"]
    for a, v in (("--chip", "esp32"), ("--port", "COMFAKE"), ("--baud", "921600"), ("--before", "default-reset"),
                 ("--after", "hard-reset"), ("--flash-mode", "keep"), ("--flash-freq", "keep"), ("--flash-size", "keep")):
        check(_arg(argv, a) == v, f"write-flash argv {a} = {_arg(argv, a)!r}, expected {v!r}")
    check("--compress" in argv, "write-flash without --compress")
    addrs = [f["address"] for f in w["files"]]
    check(addrs == [0xE000, 0x10000], f"an app-only update wrote {[hex(a) for a in addrs]}")
    ota = w["files"][0]
    check(ota["size"] == 0x2000 and ota["all_ff"], f"otadata is {ota['size']} bytes, all 0xFF {ota['all_ff']}")
    check(w["files"][1]["sha256"] == st["sha"][f"WCB_{WCB_TAG}_ESP32.bin"], "the app written is not the cached image")
    code, status = s.req("GET", "/_api/status")
    check(status.get("attached") and status.get("target") == "serial COMFAKE", f"not handed back: {status}")
    check(len(s.made) == made0 + 1 and not s.made[made0 - 1].is_open and s.made[-1].is_open,
          "the port was not released before esptool and reattached after")


@case("a full WCB flash writes bootloader, table and app; a factory reset adds NVS", group="pipeline")
def wcb_full_and_factory(ctx):
    st = _setup(ctx)
    for body, want, nvs in (({"appOnly": False}, [0x1000, 0x8000, 0xE000, 0x10000], False),
                            ({"appOnly": False, "eraseNvs": True}, [0x1000, 0x8000, 0x9000, 0xE000, 0x10000], True)):
        stt, _, runs = _flash(st, "/_api/flash-wcb", body)
        check(stt.get("ok") is True, f"{body}: {stt.get('error', '')[:300]}")
        w = _write(runs)
        addrs = [f["address"] for f in w["files"]]
        check(addrs == want, f"{body} wrote {[hex(a) for a in addrs]}, expected {[hex(a) for a in want]}")
        by = {f["address"]: f for f in w["files"]}
        check(by[0x1000]["sha256"] == st["sha"][f"WCB_{WCB_TAG}_ESP32_boot.bin"], "the bootloader is not the cached one")
        check(by[0x8000]["sha256"] == st["sha"][f"WCB_{WCB_TAG}_ESP32_part.bin"], "the table is not the cached one")
        if nvs:
            check(by[0x9000]["size"] == 0x5000 and by[0x9000]["all_ff"], "NVS was not erased as 0x5000 of 0xFF")


@case("an esptool failure comes back with esptool's own words, and the port is handed back anyway", group="pipeline")
def wcb_failures(ctx):
    st = _setup(ctx)
    s = st["server"]
    try:
        for fail, words in (("write", ("esptool failed (exit 2)", "Packet content transfer stopped")),
                            ("flash-id", ("esptool could not talk to the board", "Failed to connect"))):
            os.environ["FAKE_ESPTOOL_FAIL"] = fail
            stt, _, _ = _flash(st, "/_api/flash-wcb", {"appOnly": True})
            check(stt.get("ok") is False, f"a failing esptool ({fail}) was reported ok")
            for w in words:
                check(w in (stt.get("error") or ""), f"{fail}: the error lacks {w!r}: {(stt.get('error') or '')[:300]}")
            check(any(x.startswith("FAILED:") for x in stt.get("log") or []), f"{fail}: no FAILED line in the log")
            code, status = s.req("GET", "/_api/status")
            check(status.get("attached"), f"{fail}: the port was not handed back after the failure: {status}")
    finally:
        os.environ.pop("FAKE_ESPTOOL_FAIL", None)


@case("NaviCore's /_api/flash writes the app alone, from the cache", group="pipeline")
def navicore_app_only(ctx):
    st = _setup(ctx)
    stt, _, runs = _flash(st, "/_api/flash", {})
    check(stt.get("ok") is True, f"the NaviCore flash failed: {(stt.get('error') or '')[:300]}")
    check(stt.get("version") == NC_TAG, f"version {stt.get('version')!r}, expected {NC_TAG!r}")
    w = _write(runs)
    check(_arg(w["argv"], "--chip") == "esp32s3", f"NaviCore written as {_arg(w['argv'], '--chip')}")
    addrs = [f["address"] for f in w["files"]]
    check(addrs == [0xE000, 0x10000], f"NaviCore app-only wrote {[hex(a) for a in addrs]}")
    check(w["files"][1]["sha256"] == st["sha"][f"NaviCore_{NC_TAG}_ESP32S3.bin"], "not the cached NaviCore app")
    log = stt.get("log") or []
    check(any("flashing app only" in x for x in log), "no 'flashing app only' note for a set with no bootloader/table")


@case("flashing refuses a WiFi session and a port outside INTELLEX_SERIAL_ALLOW with 409, starting nothing",
      group="pipeline")
def refusals(ctx):
    st = _setup(ctx)
    s, b = st["server"], st["server"].host.bridge
    since = _log_len(st)
    for spec, words in (({"kind": "ws", "host": "127.0.0.1", "role": "navicore"}, "direct USB serial connection"),
                        ({"kind": "serial", "port": "COMOTHER"}, "COMOTHER is not in INTELLEX_SERIAL_ALLOW")):
        s.req("POST", "/_api/detach", {})
        b.set_target(spec)                      # straight onto the bridge: a ws attach would ask netsh for the SSID
        b.attach(s._make(spec), "test", spec)
        for route in ("/_api/flash-wcb", "/_api/flash"):
            code, j = s.req("POST", route, {"appOnly": True})
            check(code == 409 and words in (j or {}).get("error", ""), f"{route} on {spec}: {code} {j}")
    s.req("POST", "/_api/detach", {})
    check(_log_len(st) == since, "a refused flash still ran esptool")
    code, stt = s.req("GET", "/_api/flash-status")
    check(stt.get("running") is False, f"a refused flash left running={stt.get('running')}")


# ------------------------------------------------------------------ group "esptool5_should"
@case("(should) /_api/flash-status follows esptool 5.3.1's progress, and the NaviCore flash uses esptool 5's spellings "
      "(no 'Deprecated:' warnings in the log)", group="esptool5_should")
def esptool5_output(ctx):
    st = _setup(ctx)
    s = st["server"]
    s.req("POST", "/_api/attach", {"kind": "serial", "port": "COMFAKE"})
    problems = []
    os.environ["FAKE_ESPTOOL_DELAY"] = "0.25"
    try:
        stt, pcts, _ = _flash(st, "/_api/flash-wcb", {"appOnly": True}, poll=0.05)
    finally:
        os.environ.pop("FAKE_ESPTOOL_DELAY", None)
    check(stt.get("ok") is True, f"the update failed: {(stt.get('error') or '')[:300]}")
    moving = [p for p in pcts if 0 < p < 100]
    if not moving:
        problems.append(f"flash-status stayed at {sorted(set(pcts))} % through a {len(pcts)}-poll write whose esptool "
                        f"printed 25/50/75 % lines: _PCT_RE (flash.py:414) matches esptool 4's '(25 %)', not "
                        f"esptool 5.3.1's '[===>  ]  25.0%' (esptool logger.py:223-248)")
    stt, _, runs = _flash(st, "/_api/flash", {})
    w = _write(runs)
    old = [a for a in w["argv"] if a in ("write_flash", "default_reset", "hard_reset") or a.startswith("--flash_")]
    dep = [x for x in stt.get("log") or [] if "Deprecated:" in x]
    if old or dep:
        problems.append(f"the NaviCore write uses esptool 4 spellings {old} (flash.py:434-438), and esptool 5 put "
                        f"{len(dep)} 'Deprecated:' warning(s) in the user's flash log - wcb_flash.py:352-354 avoids them")
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
