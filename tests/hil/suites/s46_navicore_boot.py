"""NaviCore's boot, restart and the mesh's view of them (docs/hil_plan/NAVICORE.md NC-WP9, ids ncboot.* and sbus.boot_quiet).

Every test here restarts NaviCore in software - REBOOT over USB, '#L02', or a REBOOT relayed from W1 - so every one is
behind the navicore_reboot opt-in, except ncboot.bad_device_id, which saves an invalid mesh identity and is behind
navicore_identity (off: attended only, D-NC14). A software restart never re-samples the boot strap, so it cannot leave
the S3 in ROM download mode (NAVICORE.md §1.3). The mesh loses WCB 20 and SBUS OUT stops for about 3 s each time
(hil/servos.py lists every test here).

Rules every test here keeps:
- It skips while NaviCore's recorder holds anything (NaviCore.restart_blocker): a restart empties the buffer, and the
  buffer is bench state the harness leaves as found (docs/HIL_WEEK_DECISIONS.md D33).
- It runs inside nc_guard, even when it writes nothing: the guard proves the config, command library, clips and
  learned peers as they were after the restart, and sends a mesh SET_MODE back if the mode that came back is not the
  one it snapshotted.
- A test that saves a change that only a restart applies (boardType, the mesh quantity, the deviceId) writes the
  snapshot back and restarts again in a `finally`, so NaviCore never keeps running the changed value.
- The boot banner holds the SoftAP's name. Nothing here quotes a banner line in a note or a failure unless it is one of
  the anchors banner_anchors() marks quotable, and every first line of an error goes through redact_text (_line1).
"""
import re
import time

from hil.checkpoint import redact_text
from hil.nc_guard import nc_guard
from hil.navicore import BOOT_DONE, SBUS_FULL_FPS, NaviCore, entries, parse_boot, parse_wdp
from hil.ncflash import _await as await_line
from hil.ncflash import ota_status
from hil.ncmesh import bridged
from hil.runner import Skip, test
from hil.sbus import SbusCtl, matrix_button
from hil.wcb import WCB
from suites.common import link, marker
from suites.s40_navicore_config import FLAG_ACTIONS, _hooks

BANNER_START = "=== NaviCore ==="                                           # NaviCore.ino:4556
HOOK_LINE = ("[HIL] NAVICORE_HIL_HOOKS build: #L90-#L93 fault verbs and DBG_WIRE (debug bit 7) are live - a test image, "
             "never a release")                                          # :4559-4561, hook builds only
# Lines no healthy boot prints: the PSRAM halt, an unreadable config, the mesh or the WCB Wizard surface failing, a
# SoftAP that failed or was refused, an out-of-range baud, the empty-mesh-password box (NaviCore.ino:4486-4953).
FAILURE_MARKS = ("[FATAL]", "unreadable", "ERROR", "queue alloc failed", "FAILED", "out of range", "PASSWORD IS EMPTY",
                 "REFUSED")
JOINED = re.compile(r"^\[WCB\] Joined network as device ID (\d+) \(quantity=(\d+)\)")            # :4927-4928
ROLL_CALL = re.compile(r"^\[WCB\] roll call: (\d+)/(\d+) board\(s\) online (\d+)s after join")    # :5360-5361
NEVER_HEARD = re.compile(r"^\[WCB\] roll call: WCB(\d+)(?: · .*)? never heard from")              # :5356-5357
PEER_NEW = re.compile(r"^\[PEER\] New WCB (\d+)")                                                  # :5307
WCB_ONLINE = re.compile(r"^\[WCB\] WCB(\d+)(?: · .*)? ONLINE\s*$")                                 # onWcbStatus (:5758)
REMOTE_REBOOT = "[RC] Remote REBOOT requested via WCB"                                            # rc_telemetry.h:2406
REBOOT_ACK = {"type": "ACK", "ok": True, "msg": "rebooting"}                                       # NaviCore.ino:4032
INFO_BOARD = '{"type":"INFO","msg":"boardType changed — reboot to apply the new pin profile"}'     # :3964
PEER_GRACE_S = 8.0                     # PEER_GRACE_MS (:110): new-peer events are silent this long after join
ROLL_CALL_S = 30.0                     # ROLL_CALL_MS (:137): the roll call runs this long after join
ADVERT_WAIT_S = 8.0                    # after W1's ?WDP,POLL: every WCB advertises within 600 ms (WCB_WDP.cpp:366-372)
SW_RESET_CODE = 3                      # ESP_RST_SW: 'Software restart (incl. boot-guard retry)' (:4393)
MAESTRO_SET_TARGET = 0x04              # the Pololu protocol's setTarget command byte (0x84 with its MSB cleared)
# One TEST_ACTION whose only effect is its dispatch line under DBG_MAESTRO: a Maestro verb no Maestro knows, on remote
# slot 4 (s40 FLAG_ACTIONS; executeMaestroCmd ignores it). Proves the debug flags are on, or back at 0.
DISPATCH_PROBE = FLAG_ACTIONS[0][2]


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _line1(e):
    """The first line of an error, credentials hashed: an ExpectTimeout's next lines are the device's last lines, which
    around a restart are NaviCore's boot banner, whose SoftAP line names the AP."""
    text = str(e)
    return redact_text(text.splitlines()[0] if text else type(e).__name__)[:240]


def _restartable(nc):
    why = nc.restart_blocker()
    if why:
        raise Skip(why)


def _line_time(dev, since, rx):
    """The host time of the first line after mark `since` that matches `rx` (a pattern or a compiled one), or None."""
    pat = re.compile(rx) if isinstance(rx, str) else rx
    for t, x in list(dev.lines[since:]):
        if pat.search(x):
            return t
    return None


def _flushed(nc, since, settle=0.5):
    """The lines since mark `since`, after `settle` seconds and a '#L12' that releases a line NaviCore holds back."""
    time.sleep(settle)
    m = nc.dev.mark()
    nc.dev.send("#L12")
    try:
        nc.dev.expect(r"Mode=\d+", timeout=3, since=m)
    except AssertionError:
        pass
    return [x.rstrip() for x in nc.dev.since(since)]


def _restart(nc, how="json", timeout=30.0):
    """Restart NaviCore and wait for it -> (mark before the restart, the lines since, the REBOOT ACK or None). how:
    'json' - REBOOT, ACKed {"type":"ACK","ok":true,"msg":"rebooting"}, then ESP.restart() 250 ms later
    (NaviCore.ino:4029-4050); 'l02' - '#L02', ESP.restart() with no reply (:3623). AssertionError, first line only,
    when it does not come back (NaviCore.wait_boot)."""
    m = nc.dev.mark()
    ack = None
    if how == "json":
        ack = nc.ack({"type": "REBOOT"})
    elif how == "l02":
        nc.dev.send("#L02")
    else:
        raise ValueError(how)
    try:
        lines = nc.wait_boot(since=m, timeout=timeout)
    except AssertionError as e:
        raise AssertionError(f"NaviCore did not come back from the restart ({how}): {_line1(e)}") from None
    return m, lines, ack


def _put_back(nc, text, failure, restart=True):
    """After a body that saved a value only a restart applies: write the snapshot `text` back (SET_CONFIG) and, when
    `restart`, restart NaviCore onto it; then raise the body's own failure (`failure`, or None). A put-back that fails
    is raised after the body's failure, both named: nc_guard writes the config back too, but only a restart applies it,
    so the message says how to do one."""
    try:
        nc.set_config(text)
        if restart:
            _restart(nc, "json")
    except AssertionError as e:
        raise AssertionError((f"{failure}\n" if failure is not None else "") + f"NaviCore's saved config was not put "
                             f"back and applied: {_line1(e)}; nc_guard writes it back, and `python -m hil.ncflash reset` "
                             f"(from tests/hil, with no run holding the bench) restarts NaviCore onto it") from None
    if failure is not None:
        raise failure


def _uptime_ms(nc):
    """NaviCore's millis(), from GET_MESH_STATS page 0 (rc_telemetry.h:1371-1378): proof a restart happened when the
    banner was not captured (_restarted)."""
    return nc.mesh_stats()["upMs"]


def _restarted(up_ms, since_ms):
    """Whether NaviCore restarted after a command sent `since_ms` before its uptime read `up_ms`. Without a restart the
    uptime then is at least the time since the command (it was counting before it); after one it counts from the app's
    start, which the ROM and the bootloader put a few hundred ms after the reset. Exact whatever the uptime was before:
    a board restarted a moment earlier by another test is no special case. 100 ms covers the USB latency."""
    return up_ms < since_ms - 100


def _w1_row(w1, nid):
    """W1's ?WDP,DUMP row for board `nid` (WCB_WDP.cpp:1823-1827) as a dict of strings, or None."""
    return next((r for r in parse_wdp(w1.run("?WDP,DUMP", timeout=8))["rows"] if r.get("N") == str(nid)), None)


def _fresh_w1_row(w1, nid, since, timeout=20.0):
    """(W1's row for `nid`, fresh) once the row's advert was heard after host time `since`, polled every 2 s; the last
    row read and False when none came in `timeout` (None when there was no row). AGE counts whole seconds since W1
    heard the last advert, so 'AGE below the time since `since`' puts that advert at most a second before it: the
    advert NaviCore sent before a restart is tens of seconds older (a WCB_Client advert goes out about once a minute)."""
    deadline = time.monotonic() + timeout
    row = None
    while True:
        row = _w1_row(w1, nid) or row
        if row and str(row.get("AGE", "")).isdigit() and int(row["AGE"]) < time.monotonic() - since:
            return row, True
        if time.monotonic() >= deadline:
            return row, False
        time.sleep(2.0)


# ------------------------------------------------------------------ the boot banner (pure: selftest.py feeds it lines)
def _san(baud):
    """sanBaud's range (NaviCore.ino:3241-3246): a stored baud outside 1200-115200 opens at a default instead, and the
    banner then names that default after an '[AUX] ... out of range' line."""
    return baud if isinstance(baud, int) and 1200 <= baud <= 115200 else None


def banner_anchors(cfg, version, app_sha, hooks):
    """The lines setup() prints (NaviCore.ino:4486-4953), in order, for GET_CONFIG `cfg`, the version PING answers, the
    App SHA256 ?OTALOCAL,STATUS printed and whether the image is a hook build -> [(label, check(line) -> bool, shown)].
    `shown` is the expected text for a failure message, or None for the SoftAP line, which names the AP: its SSID and
    channel are compared in memory and never printed. Library lines ([WCB_Client] ...) other than the WDP identity are
    not anchored: they belong to WCB_Client, whose wording is not NaviCore's."""
    net = cfg.get("wcbNetwork") or {}
    aux = cfg.get("auxBaud") or {}
    v32 = cfg.get("boardType") == 1                  # applyBoardProfile (:3199-3218): 1 is WCB HW 3.2, anything else v2
    rx, tx = (5, 4) if v32 else (4, 5)

    def eq(want):
        return want, (lambda x: x == want)

    def like(shown, pattern):
        r = re.compile(pattern)
        return shown, (lambda x: r.fullmatch(x) is not None)

    def baud(key):
        b = _san(aux.get(key))
        return str(b) if b is not None else r"\d+"
    out = [("the banner's first line", *eq(BANNER_START))]
    if app_sha:                                      # an image from before INF9 (a) prints no SHA line at all
        out.append(("App SHA256", *eq(f"App SHA256: {app_sha}")))
    if hooks:
        out.append(("the hook-build line", *eq(HOOK_LINE)))
    out += [("the bootloader", *like("Bootloader: CUSTOM short-WDT ... / stock (IDF ...) / unknown ...",
                                      r"Bootloader: (CUSTOM short-WDT .*|stock \(IDF .*\)|unknown \(no description block\))")),
            ("reset reason 3", *like("Reset reason: 3 - Software restart (incl. boot-guard retry)  (RTC codes ...)",
                                     r"Reset reason: 3 - Software restart \(incl\. boot-guard retry\)  \(RTC codes .*\)")),
            ("the boot counter", *like("Boot attempts since power applied: <n>",
                                       r"Boot attempts since power applied: \d+(   <-- board retried/reset before this "
                                       r"boot)?")),
            ("rcConfig in PSRAM", *like("[MEM] rcConfig (<n> bytes) allocated in PSRAM, free PSRAM now <n>",
                                        r"\[MEM\] rcConfig \(\d+ bytes\) allocated in PSRAM, free PSRAM now \d+")),
            ("the config from LittleFS", *eq("RC config loaded from LittleFS.")),
            ("the clips partition", *like("[CLIPS] mounted: <n> KB free of 12288 KB",
                                          r"\[CLIPS\] mounted: \d+ KB free of 12288 KB")),
            ("the pin profile", *eq(f"[BOARD] {'WCB HW 3.2' if v32 else 'NaviCore v2'} pin profile")),
            ("SBUS on UART1", *eq(f"[SBUS] IN+OUT share Serial1/UART1 — RX GPIO{rx} / TX GPIO{tx}, 100k 8E2 inverted. "
                                  f"UART0 = hardware S3.")),
            ("the Maestro port", *like(f"[Serial2] Local Maestro open @ {baud('maestro')} baud  TX=GPIO6",
                                       rf"\[Serial2\] Local Maestro open @ {baud('maestro')} baud  TX=GPIO6")),
            ("S3", *like(f"[AUX] S3 open @ {baud('S3')} baud (hw UART0)",
                         rf"\[AUX\] S3 open @ {baud('S3')} baud \(hw UART0\)")),
            ("S4", *like(f"[AUX] S4 open @ {baud('S4')} baud", rf"\[AUX\] S4 open @ {baud('S4')} baud")),
            ("S5", *like(f"[AUX] S5 open @ {baud('S5')} baud", rf"\[AUX\] S5 open @ {baud('S5')} baud")),
            ("SBUS OUT", *eq(f"[SBUS] OUT enabled — re-emit on GPIO{tx} (100k 8E2 inverted)" if cfg.get("sbusOutEnabled")
                             else "[SBUS] OUT disabled (passthrough off — no CPU cost)"))]
    if cfg.get("wifiEnabled"):                       # the SoftAP block (:4711-4789)
        pw = (cfg.get("wifiPassword") or "").encode("utf-8")
        if 0 < len(pw) < 8:
            out.append(("the AP refused (a short password)", *like("[WIFI] REFUSED: password is <n> character(s); ...",
                                                                    r"\[WIFI\] REFUSED: password is \d+ character\(s\); "
                                                                    r"WPA2 requires 8\.")))
        elif not pw:
            out.append(("the AP refused (no password)", *like("[WIFI] REFUSED: no AP password set. ...",
                                                               r"\[WIFI\] REFUSED: no AP password set\. .*")))
        else:
            ssid = (cfg.get("wifiSsid") or f"NaviCore-{net.get('deviceId')}").encode("utf-8")[:32]
            ap = re.compile(r'\[WIFI\] SoftAP "(.*)" up on channel (\d+) — \d+\.\d+\.\d+\.\d+')

            def softap(x, ssid=ssid, ch=net.get("channel")):
                m = ap.fullmatch(x)
                return bool(m) and m.group(1).encode("utf-8") == ssid and int(m.group(2)) == ch
            out += [("the SoftAP on the mesh channel", None, softap),
                    ("ESP-NOW on the AP's channel", *eq("[WIFI] ESP-NOW will share this channel (WIFI_AP_STA).")),
                    ("DHCP with no gateway", *eq("[WIFI] DHCP offers no default gateway — clients keep their own "
                                                 "route.")),
                    ("the WebSocket endpoint", *like("[WS] command endpoint ready — ws://<ip>/ws",
                                                     r"\[WS\] command endpoint ready — ws://\d+\.\d+\.\d+\.\d+/ws"))]

    def done(x):
        m = BOOT_DONE.search(x)
        return bool(m) and m.group(1) == version
    out += [("the WDP identity", *eq(f'[WCB_Client] WDP identity set: type="NaviCore" fw="{version}"')),
            ("the mesh join", *eq(f"[WCB] Joined network as device ID {net.get('deviceId')} "
                                  f"(quantity={net.get('quantity')})")),
            ("setup complete", f"[NaviCore] Firmware {version} — setup complete.", done)]
    return [(label, check, shown) for label, shown, check in out]


def banner_lines(lines):
    """setup()'s banner out of the lines since a restart: from the last '=== NaviCore ===' through the 'setup complete.'
    line, right-stripped; [] when there is no start line (lost to a USB re-enumeration, or no restart)."""
    xs = [x.rstrip() for x in lines]
    starts = [i for i, x in enumerate(xs) if x == BANNER_START]
    if not starts:
        return []
    tail = xs[starts[-1]:]
    end = next((i for i, x in enumerate(tail) if BOOT_DONE.search(x)), None)
    return tail[:end + 1] if end is not None else tail


def banner_problems(banner, anchors):
    """-> (problems, facts). Each anchor must match a line after the one the anchor before it matched: a missing one
    and an out-of-order one are named, with the expected text only when it may be shown. A line no anchor claims fails
    when it carries a FAILURE_MARKS word (quoted unless it is a [WIFI] line, which names the AP). facts: 'bootloader'
    (stock / CUSTOM short-WDT / unknown), and parse_boot's reset, rtc and attempts."""
    problems, claimed, i = [], set(), 0
    for label, check, shown in anchors:
        j = next((k for k in range(i, len(banner)) if check(banner[k])), None)
        if j is None:
            before = next((k for k in range(0, i) if check(banner[k])), None)
            what = "out of order" if before is not None else "missing"
            problems.append(f"{label}: {what}" + (f" (expected {shown!r})" if shown else ""))
            continue
        claimed.add(j)
        i = j + 1
    for k, x in enumerate(banner):
        if k in claimed or not any(w in x for w in FAILURE_MARKS):
            continue
        problems.append("a [WIFI] failure line (not quoted: it names the AP)" if x.startswith("[WIFI]")
                        else f"a failure line: {redact_text(x)[:140]!r}")
    boot = parse_boot(banner)
    kind = next((x.split(":", 1)[1].strip().split(" ")[0] for x in banner if x.startswith("Bootloader:")), None)
    return problems, {"bootloader": kind, "reset": boot["reset_code"], "rtc": boot["rtc"], "attempts": boot["attempts"]}


# ============================================================ the banner and what a restart resets
@test("ncboot.banner_order", "After a REBOOT, setup()'s banner in order: App SHA256 equal to STATUS's, the hook line on a "
      "hook build, the bootloader (recorded), reset reason 3, the boot counter, rcConfig in PSRAM, the config from "
      "LittleFS, the 12 MB clips partition, the pin profile, SBUS on UART1, the Maestro and S3-S5 ports at GET_CONFIG's "
      "bauds, SBUS OUT, the SoftAP on the mesh channel, the WDP identity, the mesh join with GET_CONFIG's deviceId and "
      "quantity, 'setup complete' with PONG's version; no failure line (NaviCore restarts)",
      needs=["navicore"], links=[], opt_in="navicore_reboot")
def banner_order(bench):
    """The map's nc.boot.banner, nc.sbus.uart, nc.mae.serial2, nc.aux.binding, nc.board.profile, nc.mesh.join_boot and
    nc.mesh.channel rows, read from one restart (banner_anchors has the lines and their sources). The banner arrives
    whole on this bench: the S3's USB-Serial/JTAG port does not drop across a software restart (every NaviCore restart
    in results/builds/ncflash-logs shows it from the ROM's first line on). The SSID and the channel of the SoftAP line
    are compared with GET_CONFIG in memory; that line is never quoted. The bootloader kind is recorded, not asserted:
    the bench board runs the stock IDF bootloader (every banner on 2026-09-28), while a board the config tool flashed
    carries the custom short-watchdog one (printBootloaderInfo :4360-4379)."""
    nc = _nc(bench)
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        version = g.nc.ping()
        st = ota_status(g.nc)
        hooks = _hooks(g.nc)
        anchors = banner_anchors(g.before, version, st["app_sha"], hooks)
        _, lines, ack = _restart(g.nc, "json")
        banner = banner_lines(lines)
        if not banner:
            problems.append("no boot banner arrived after the REBOOT (not from '=== NaviCore ===' on)")
        else:
            problems, facts = banner_problems(banner, anchors)
        if ack != REBOOT_ACK:
            problems.append(f"REBOOT answered {ack}, not {REBOOT_ACK}")
    bench.note(f"banner: {len(banner)} lines, {len(anchors)} anchors; {facts}; hook build {hooks}")
    assert not problems, "; ".join(problems)


def _ram_state_on(nc):
    """Turn on what a restart must turn off - every debug flag and the monitor - and prove both took -> problems."""
    nc.set_debug_flags(0x7F)
    nc.ack({"type": "START_MONITOR"})
    m = nc.dev.mark()
    nc.test_action(DISPATCH_PROBE)
    lines = _flushed(nc, m, settle=0.6)
    out = []
    if not any(x.startswith("[DISPATCH] Maestro") for x in lines):
        out.append("with every debug flag set, the probe action printed no [DISPATCH] Maestro line")
    if not any(x.startswith('{"type":"PWM_UPDATE"') for x in lines):
        out.append("START_MONITOR streamed no PWM_UPDATE frame")
    return out


def _ram_state_after(nc):
    """After a restart -> problems: a PWM_UPDATE frame in 1.5 s (the monitor survived), or the probe action's
    [DISPATCH] line (a debug flag survived)."""
    m = nc.dev.mark()
    time.sleep(1.5)
    out = []
    frames = [x for x in nc.dev.since(m) if x.startswith('{"type":"PWM_UPDATE"')]
    if frames:
        out.append(f"{len(frames)} PWM_UPDATE frames in 1.5 s: the monitor survived the restart")
    m = nc.dev.mark()
    ack = nc.test_action(DISPATCH_PROBE)
    lines = _flushed(nc, m, settle=0.6)
    if any(x.startswith("[DISPATCH]") for x in lines):
        out.append("the probe action still printed a [DISPATCH] line: a debug flag survived the restart")
    # A verb no Maestro knows: TEST_ACTION skips it and says why (D-NC20); an answer at all shows NaviCore is up
    if ack.get("of") != "TEST_ACTION" or ack.get("ok") is not False or not ack.get("msg"):
        out.append(f"the probe TEST_ACTION was answered {ack}, not ok:false with its msg (D-NC20)")
    return out


@test("ncboot.reboot_resets_ram_state", "REBOOT (ACKed 'rebooting') and '#L02' (no reply) each restart NaviCore - its "
      "uptime starts again, the banner names reset 3 - and each clears what lives in RAM: with every debug flag set "
      "and the monitor streaming before, none of either after (NaviCore restarts twice)",
      needs=["navicore"], links=[], opt_in="navicore_reboot")
def reboot_resets_ram_state(bench):
    """REBOOT answers {"type":"ACK","ok":true,"msg":"rebooting"}, flushes, and restarts 250 ms later
    (NaviCore.ino:4029-4050); #L02 is ESP.restart() at once, with no reply (:3623). g_dbgFlags (:495) and
    wsMonitorActive (:478) are plain RAM, 0/false at boot. A flag is shown on or off by one TEST_ACTION whose only effect
    is its [DISPATCH] line (s40 FLAG_ACTIONS: a Maestro verb no Maestro knows, on remote slot 4); the monitor by its
    PWM_UPDATE frames. CALIB is not tested here: every PING clears it (:3835-3838), and wait_boot PINGs. Whether the USB
    port dropped and reopened is noted: on this bench it has not (results/builds/ncflash-logs). If a restart never
    happens, nc_guard's restore sends SET_DEBUG_FLAGS 0 and STOP_MONITOR before anything else (hil/nc_guard.py _quiet)."""
    nc = _nc(bench)
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        for how in ("json", "l02"):
            problems += [f"{how}: {p}" for p in _ram_state_on(g.nc)]
            up0 = _uptime_ms(g.nc)
            t0 = time.monotonic()
            _, lines, ack = _restart(g.nc, how)
            up1, since_ms = _uptime_ms(g.nc), (time.monotonic() - t0) * 1000
            boot = parse_boot(lines)
            if how == "json" and ack != REBOOT_ACK:
                problems.append(f"REBOOT answered {ack}, not {REBOOT_ACK}")
            if not _restarted(up1, since_ms):
                problems.append(f"{how}: uptime {up1} ms, {since_ms:.0f} ms after the command (was {up0} ms): "
                                f"NaviCore did not restart")
            if boot["reset_code"] is not None and boot["reset_code"] != SW_RESET_CODE:
                problems.append(f"{how}: the banner names reset {boot['reset_code']} ({boot['reset']}), not 3")
            problems += [f"{how}: {p}" for p in _ram_state_after(g.nc)]
            facts[how] = dict(reset=boot["reset_code"], rtc=boot["rtc"], up_ms=up1,
                              reopened=any(x.startswith("<<reopened") for x in lines))
    bench.note(f"restarts: {facts}")
    assert not problems, "; ".join(problems)


CLEAN_REBOOTS = 20     # ncboot.reboots_clean: at the old one-boot-in-five panic rate, 20 clean in a row is 2 in 100


@test("ncboot.reboots_clean", "20 REBOOTs in a row each boot NaviCore once and cleanly: every banner names reset 3 "
      "(software restart) and no panic line comes between a REBOOT and NaviCore answering again (NaviCore restarts 20 "
      "times, ~1.5 min)", needs=["navicore"], links=[], opt_in="navicore_reboot")
def reboots_clean(bench):
    """NaviCore 2c698c6 (D-NC24) installed the soft ports' GPIO ISR service after SBUS had started streaming into
    UART1. ESP-IDF registers it through the core's IPC task, and an interrupt pending across the registration's heap
    critical section overflowed that task's 1 KB stack: 'Guru Meditation Error: Core 1 panic'ed (Unhandled debug
    exception)' in _frxt_int_enter on ipc1, then a second boot reporting 'Crash (panic)' - 8 of 45 boots in full runs
    20261006-122850 and -235930, none of 18 before 2c698c6. NaviCore 6bd0ced installs it first in setup(). A run of
    restarts catches a rate of that order: at the old 18 %, 20 clean boots in a row happen about 2 times in 100."""
    nc = _nc(bench)
    _restartable(nc)
    problems, codes = [], []
    with nc_guard(bench, nc=nc) as g:
        for i in range(CLEAN_REBOOTS):
            _, lines, _ = _restart(g.nc, "json")
            boot = parse_boot(lines)
            codes.append(boot["reset_code"])
            panics = [x for x in lines if "Guru Meditation" in x or "abort() was called" in x]
            if panics:
                problems.append(f"restart {i + 1}: {panics[0][:150]!r}")
            elif boot["reset_code"] is not None and boot["reset_code"] != SW_RESET_CODE:
                problems.append(f"restart {i + 1}: the banner names reset {boot['reset_code']} ({boot['reset']})")
    bench.note(f"ncboot.reboots_clean: reset codes {codes}")
    assert not problems, f"{len(problems)} of {CLEAN_REBOOTS} restarts were not clean: " + "; ".join(problems[:4])


# ============================================================ the mesh across a restart
@test("ncboot.wcbs_see_reboot", "After a NaviCore REBOOT the mesh has it back within seconds: W1 prints '[ETM] WCB20 came "
      "ONLINE (boot)', W1's WDP row for 20 holds an advert from after the restart with PONG's version, ;W20,?version "
      "is answered as [TERM:20], a WCB_SEND to W1 lands, and 30 s after the join the roll call counts every floor board "
      "online with none 'never heard from' (NaviCore restarts)", needs=["navicore", "wcb1"], links=[],
      opt_in="navicore_reboot")
def wcbs_see_reboot(bench):
    """WCB_Client sends three boot announces after begin() (WCB_Client.cpp:78, :1819-1829), which W1 answers with
    '[ETM] WCB<n> came ONLINE (boot)' and a cleared duplicate ring for the sender (WCB.ino:5282-5300), so NaviCore's
    reset sequence numbers are not dropped as repeats. Its boot advert burst refreshes W1's row (WCB_WDP.cpp:1823, AGE
    in whole seconds since the last advert). The relayed terminal (;W20,?version, [TERM:20]) and a NaviCore->W1 unicast
    (WCB_SEND ';S0,<marker>', which W1 echoes on its console) prove both directions. The roll call (checkBootRollCall
    NaviCore.ino:5343-5362) runs once, 30 s after the join, over the floor 1..quantity: on this bench (quantity 1) that
    is W1 alone; ncboot.roll_call_missing_board covers a board it names."""
    nc, w1 = _nc(bench), WCB(bench.dev("wcb1"))
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        fw = g.nc.ping()
        nid = g.nc.wcb_status()["self"]
        wm = w1.dev.mark()
        t0 = time.monotonic()
        m, lines, _ = _restart(g.nc, "json")
        t_join = _line_time(g.nc.dev, m, JOINED) or time.monotonic()
        try:        # not anchored: the WiFi task's line can land glued to another (docs/HIL_TEST_AUDIT.md F18)
            w1.dev.expect(rf"\[ETM\] WCB{nid} came ONLINE \(boot\)", timeout=12, since=wm)
        except AssertionError:
            problems.append(f"W1 printed no '[ETM] WCB{nid} came ONLINE (boot)' within 12 s")
        row, fresh = _fresh_w1_row(w1, nid, t0)
        facts["w1_row_age"] = row.get("AGE") if row else None
        if not row or not fresh:
            problems.append(f"W1's WDP row for {nid} holds no advert from after the restart (AGE "
                            f"{row.get('AGE') if row else 'no row'} s, {time.monotonic() - t0:.0f} s since)")
        elif row.get("FW") != fw:
            problems.append(f"W1's row for {nid} names firmware {row.get('FW')}, PONG {fw}")
        tm = w1.dev.mark()
        w1.send(f";W{nid},?version")
        try:
            w1.dev.expect(rf"^\[TERM:{nid}\]Software Version: {re.escape(fw)}$", timeout=6, since=tm)
        except AssertionError:
            problems.append(f";W{nid},?version got no [TERM:{nid}] reply on W1 within 6 s")
        mk = marker("BOOT")
        sm = w1.dev.mark()
        ack = g.nc.wcb_send(1, f";S0,{mk}")
        try:
            w1.dev.expect(rf"^{mk}", timeout=5, since=sm)
        except AssertionError:
            problems.append(f"NaviCore's WCB_SEND ;S0 to W1 (ACK {ack}) never reached W1's console")
        rc = await_line(g.nc.dev, m, ROLL_CALL, max(1.0, t_join + ROLL_CALL_S + 10 - time.monotonic()), 0.5)
        after = _flushed(g.nc, m)
        missing = sorted({int(n.group(1)) for x in after for n in [NEVER_HEARD.match(x)] if n})
        facts["roll_call"] = rc.group(0) if rc else None
        if rc is None:
            problems.append(f"no '[WCB] roll call' line {ROLL_CALL_S:.0f} s after the join")
        elif rc.group(1) != rc.group(2) or missing:
            problems.append(f"the roll call counted {rc.group(1)}/{rc.group(2)} boards online and named WCB {missing} "
                            f"never heard from")
    bench.note(f"after the restart: {facts}")
    assert not problems, "; ".join(problems)


@test("ncboot.new_peer_after_boot", "(should) After a restart NaviCore does not treat the WCBs that never left as new "
      "peers: no '[PEER] New WCB' line and no new-peer action (here a ;S2 marker to W1S2) when their next adverts arrive "
      "(NaviCore restarts)", needs=["navicore", "wcb1"],
      opt_in="navicore_reboot")
def new_peer_after_boot(bench):
    """NAVICORE.md D-NC25. drainPeerEvents (NaviCore.ino:5708) records a board silently when its first advert of the
    session lands inside PEER_GRACE_MS (8 s, :110) of the join, or when it is ETM-online the moment the grace ends; a
    WCB advertises every 60 s, so without the second rule every present WCB's next advert fired the alert and the
    configured peer actions - a sound or a servo on a user's droid, for a board that never left. Here the actions are
    one guarded wcb_unicast of ';S2<marker>' to W1, which lands on the W1S2 probe; the grace is waited out and W1's
    ?WDP,POLL makes every WCB advertise at once instead of within a minute (WCB_WDP.cpp:366-372). The boards count as
    never having left when GET_WCB_STATUS shows them online 1.5 s after the grace. NaviCore hears each one again at its
    next ETM heartbeat, 0-11 s after the join (one every 9-11 s, at its own phase), so the failure names the boards first
    heard after the grace: the ones its snapshot missed. The peer-event config is the guard's to restore."""
    s12 = link(bench, 1, "S2")
    nc, w1 = _nc(bench), WCB(bench.dev("wcb1"))
    _restartable(nc)
    mk = marker("PEER")
    events, markers, online, heard = [], 0, set(), {}
    with nc_guard(bench, nc=nc) as g:
        nid = g.nc.wcb_status()["self"]
        g.nc.set_config({"peerEvent": {"alert": True, "actions": [{"type": "wcb_unicast", "target": "1",
                                                                   "cmd": f";S2{mk}"}]}})
        pm = s12.mark()
        m, _, _ = _restart(g.nc, "json")
        t_join = _line_time(g.nc.dev, m, JOINED) or time.monotonic()
        time.sleep(max(0.0, t_join + PEER_GRACE_S + 1.5 - time.monotonic()))
        online = g.nc.online_ids() - {nid}
        w1.run("?WDP,POLL", timeout=6)
        time.sleep(ADVERT_WAIT_S)
        after = _flushed(g.nc, m)
        events = sorted({int(p.group(1)) for x in after for p in [PEER_NEW.match(x)] if p})
        markers = s12.received(pm).count(mk.encode())
        for t, x in list(g.nc.dev.lines[m:]):                     # each board's first '[WCB] WCBn ONLINE' since the join
            o = WCB_ONLINE.match(x)
            if o and int(o.group(1)) not in heard:
                heard[int(o.group(1))] = t - t_join
    fired = sorted(set(events) & online)
    when = ", ".join(f"W{b} {s:.1f} s" for b, s in sorted(heard.items()))
    bench.note(f"online {PEER_GRACE_S + 1.5:.1f} s after the join: {sorted(online)} (first heard after it: "
               f"{when or 'no ONLINE lines'}); [PEER] New WCB lines for {events}; markers on W1S2: {markers}")
    late = {b: heard[b] for b in fired if heard.get(b, 0.0) >= PEER_GRACE_S}
    gap = (f"; it first heard {', '.join(f'WCB{b} {s:.1f} s' for b, s in late.items())} after the join, past its "
           f"{PEER_GRACE_S:.0f} s grace, which records only the boards online by then, while a WCB heartbeats every "
           f"9-11 s" if late else "")
    assert not fired and not markers, (
        f"(should, D-NC25) NaviCore fired its new-peer alert for WCB {fired}, which never left (PEER_GRACE_MS, "
        f"NaviCore.ino:110), and its configured action ran {markers} time(s){gap}: a restart re-fires a user's peer "
        f"actions for boards that never left")


@test("ncboot.roll_call_missing_board", "With the mesh floor (quantity) raised to take in an absent board, the boot roll "
      "call names it 'never heard from' and counts the others online 30 s after the join; the quantity is written back "
      "and NaviCore restarted again on it (NaviCore restarts twice)", needs=["navicore"], links=[],
      opt_in="navicore_reboot")
def roll_call_missing_board(bench):
    """checkBootRollCall (NaviCore.ino:5343-5362) names each board in 1..quantity, itself aside, that is not online 30 s
    after the join. The plan's version, W2 deafened across the boot, cannot show it on this bench: the floor is
    quantity 1, so W2 is never in the roll call, and ncmesh.deaf stops a board's reception, not its heartbeats
    (hil/ncmesh.py deaf). So the floor is raised, inside nc_guard, to the lowest id above it that no board uses - not
    one NaviCore hears or lists, and not a bench WCB - (3 on a bench of W1 and W2: both then online, WCB3 absent; 4
    with a real WCB3 online in the floor too), and NaviCore restarts on it (the quantity is read once, into WCB_Client,
    at boot, :4834-4838). What that changes for ~40 s: NaviCore pre-registers that board as a peer and tracks it for
    ensured broadcasts. What it does not: the learned-peer record in NVS (_loadLearnedPeers skips a learned id the
    floor covers and saves nothing, WCB_Client.cpp:1745-1774), the mesh identity (deviceId, channel, password) and the
    octets. The snapshot goes back and NaviCore restarts onto quantity 1 however the body ended (_put_back)."""
    nc = _nc(bench)
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        net = g.before["wcbNetwork"]
        q, me = net["quantity"], net["deviceId"]
        heard = g.nc.online_ids()
        rows = {int(r["N"]) for r in g.nc.wdp_dump() if str(r.get("N", "")).isdigit()}
        used = heard | rows | set(bench.wcb_numbers())
        absent = next((n for n in range(q + 1, 20) if n != me and n not in used), None)
        if absent is None:
            raise Skip(f"no board id above the floor ({q}) that no board uses")
        floor = [n for n in range(1, absent + 1) if n != me]
        silent = [n for n in floor if n != absent and n not in heard]
        if silent:
            raise Skip(f"WCB {silent} would be in the raised floor but is not online now: the roll call would name it too")
        failure = None
        try:
            g.nc.set_config({"wcbNetwork": {"quantity": absent}})
            m, _, _ = _restart(g.nc, "json")
            t_join = _line_time(g.nc.dev, m, JOINED) or time.monotonic()
            rc = await_line(g.nc.dev, m, ROLL_CALL, max(1.0, t_join + ROLL_CALL_S + 10 - time.monotonic()), 0.5)
            after = _flushed(g.nc, m)
            named = sorted({int(n.group(1)) for x in after for n in [NEVER_HEARD.match(x)] if n})
            joined = next((j.groups() for x in after for j in [JOINED.match(x)] if j), None)
            facts = dict(quantity=absent, joined=joined, roll_call=rc.group(0) if rc else None, named=named)
            if joined != (str(me), str(absent)):
                problems.append(f"NaviCore joined as {joined}, not device {me} with quantity {absent}")
            if rc is None:
                problems.append(f"no '[WCB] roll call' line {ROLL_CALL_S:.0f} s after the join")
            elif (int(rc.group(1)), int(rc.group(2))) != (len(floor) - 1, len(floor)):
                problems.append(f"the roll call counted {rc.group(1)}/{rc.group(2)} online, expected "
                                f"{len(floor) - 1}/{len(floor)}")
            if named != [absent]:
                problems.append(f"the roll call named {named} never heard from, expected [{absent}]")
        except Exception as e:  # noqa: BLE001 - raised by _put_back once the quantity is back
            failure = e
        _put_back(g.nc, g.before_text, failure)
    bench.note(f"roll call with the floor raised: {facts}")
    assert not problems, "; ".join(problems)


@test("ncboot.mesh_reboot", "(should) A REBOOT relayed from W1 (;W20,{\"type\":\"REBOOT\"}) is answered with a JSON ACK on "
      "W1 before NaviCore restarts, once (NaviCore restarts)", needs=["navicore", "wcb1"], links=[],
      opt_in="navicore_reboot")
def mesh_reboot(bench):
    """NAVICORE.md D-NC29. rcTelemetry::handle's REBOOT branch (rc_telemetry.h:2404-2410) prints '[RC] Remote REBOOT
    requested via WCB', waits 100 ms and calls ESP.restart() on the ESP-NOW receive callback, with no reply: a tool that
    sent it cannot tell a restart from a lost packet, and anything queued behind it is lost (the WCB's rule 11: ACK,
    then restart from loop() once the queue is quiet). WCB_Client ACKs the ETM packet before the callback runs
    (WCB_Client.cpp:2821-2829), so W1 stops retrying and there is one restart, which is checked first: it is today's
    behaviour, and a second restart would be a worse bug. The (should) part is the JSON ACK on W1, before the restart."""
    nc, w1 = _nc(bench), WCB(bench.dev("wcb1"))
    _restartable(nc)
    problems = []
    with nc_guard(bench, nc=nc) as g:
        up0 = _uptime_ms(g.nc)
        m, wm = g.nc.dev.mark(), w1.dev.mark()
        t0 = time.monotonic()
        reply = bridged(w1, {"type": "REBOOT"}, r'^\{"sys":1,"type":"ACK"', timeout=4.0)
        try:
            g.nc.wait_boot(since=m, timeout=30)
        except AssertionError as e:
            raise AssertionError(f"NaviCore did not come back from the relayed REBOOT: {_line1(e)}") from None
        time.sleep(5.0)
        lines = _flushed(g.nc, m)
        up1, since_ms = _uptime_ms(g.nc), (time.monotonic() - t0) * 1000
        requests = sum(1 for x in lines if x == REMOTE_REBOOT)
        resets = sum(1 for x in lines if x.startswith("Reset reason:"))
        t_ack = _line_time(w1.dev, wm, r'^\{"sys":1,"type":"ACK"')
        t_rst = _line_time(g.nc.dev, m, r"^(ESP-ROM:|Reset reason:)")
    # The line itself is not evidence: it is printed on the receive callback 100 ms before ESP.restart(), and
    # NaviCore's USB-Serial/JTAG holds a short line until more output follows it, so the restart usually takes it
    # (run 20260928-212848: none arrived, and the uptime proved the one restart). Only a second one would say much.
    if requests > 1:
        problems.append(f"NaviCore printed {requests!r} '{REMOTE_REBOOT}' lines for one relayed REBOOT")
    if not _restarted(up1, since_ms):
        problems.append(f"NaviCore's uptime is {up1} ms, {since_ms:.0f} ms after the REBOOT (was {up0} ms): it did not "
                        f"restart")
    if resets > 1:
        problems.append(f"{resets} restarts followed one relayed REBOOT")
    assert not problems, "; ".join(problems)
    assert reply.match is not None and (t_rst is None or t_ack is None or t_ack < t_rst), (
        "(should, D-NC29) a REBOOT relayed from W1 gets no JSON ACK: rc_telemetry.h:2404-2410 restarts inline on the "
        "receive callback 100 ms after '[RC] Remote REBOOT requested via WCB', so the sender cannot tell a restart "
        "from a lost packet and whatever was queued behind it is lost; ACK it, then restart from loop() once the queue "
        "is quiet")


def _hwrev_after(w1, nid, since):
    """(HWREV W1 holds for `nid` from an advert heard after host time `since`, whether the row was that fresh)."""
    row, fresh = _fresh_w1_row(w1, nid, since)
    return (row or {}).get("HWREV"), fresh


@test("ncboot.boardtype2_mismatch", "(should) boardType 2 is refused or clamped to 0-1 on the way in; today it is stored "
      "and boots the v2 pins while the WDP advert names 'WCB 3.2', which the test shows after one restart on it and "
      "then puts right (NaviCore restarts twice)", needs=["navicore", "wcb1"], links=[], opt_in="navicore_reboot")
def boardtype2_mismatch(bench):
    """NAVICORE.md D-NC19. SET_CONFIG stores boardType unchecked (rc_config.h:1532), and three readers disagree on 2:
    applyBoardProfile takes only 1 as WCB HW 3.2 (NaviCore.ino:3199-3218), so 2 boots the v2 pins, and #L01 names the
    applied profile the same way (:3621-3622); the WDP identity takes anything but 0 as 'WCB 3.2' (:4905-4907), and so do
    the aux port labels (auxPortLabel :274-279). Safe on this board: 2 boots exactly the pins 0 does. It is never 1,
    whose pins differ: a stored value other than 2 is written back without a restart. After a restart on 2 the snapshot
    goes back and NaviCore restarts onto it however the body ended (_put_back), which also re-advertises 'NaviCore v2'."""
    nc, w1 = _nc(bench), WCB(bench.dev("wcb1"))
    _restartable(nc)
    evidence, stored, notes = {}, None, []
    with nc_guard(bench, nc=nc) as g:
        if g.before.get("boardType") != 0:
            raise Skip(f"NaviCore's boardType is {g.before.get('boardType')}, not 0 (the NaviCore v2 profile)")
        nid = g.before["wcbNetwork"]["deviceId"]
        m = g.nc.dev.mark()
        ack = g.nc.set_config({"boardType": 2}, check=False)
        saved = [x.rstrip() for x in g.nc.dev.since(m)]
        stored = g.nc.config().get("boardType")
        evidence["ack_ok"], evidence["info_line"] = ack.get("ok"), INFO_BOARD in saved
        if stored != 2:
            if stored != g.before.get("boardType"):
                _put_back(g.nc, g.before_text, None, restart=False)      # never restart onto another profile
        else:
            failure = None
            try:
                t0 = time.monotonic()
                _, lines, _ = _restart(g.nc, "json")
                evidence["banner"] = next((x.rstrip() for x in lines if x.startswith("[BOARD]")), None)
                evidence["l01"] = next((x.rstrip() for x in g.nc.cli("#L01") if x.startswith("NaviCore —")), None)
                evidence["hwrev"], fresh = _hwrev_after(w1, nid, t0)
                if not fresh:
                    notes.append("W1 heard no advert from NaviCore after the restart: its HWREV is the older one")
            except Exception as e:  # noqa: BLE001 - raised by _put_back once boardType is back
                failure = e
            t1 = time.monotonic()
            _put_back(g.nc, g.before_text, failure)
            hwrev, fresh = _hwrev_after(w1, nid, t1)
            evidence["hwrev_after_restore"] = hwrev if fresh else None
            if not fresh:
                notes.append("W1 heard no advert from NaviCore after the restoring restart")
    bench.note(f"boardType 2: stored as {stored}; {evidence}" + (f"; {'; '.join(notes)}" if notes else ""))
    if evidence.get("hwrev_after_restore") not in (None, "NaviCore v2"):
        raise AssertionError(f"after the restore and a restart W1 still holds HWREV {evidence['hwrev_after_restore']!r} "
                             f"for NaviCore")
    assert stored in (0, 1), (
        f"(should, D-NC19) boardType 2 is stored as sent (rc_config.h:1532); after a restart on it NaviCore booted "
        f"{evidence.get('banner')!r}, #L01 said {evidence.get('l01')!r} and its WDP advert says HWREV "
        f"{evidence.get('hwrev')!r} (NaviCore.ino:4905-4907): clamp it to 0-1 on input")


# ============================================================ attended: a saved mesh identity that fails
@test("ncboot.bad_device_id", "OPT-IN (navicore_identity, attended): a saved deviceId 21 fails the mesh at the next boot - "
      "WCB_Client refuses it (out of range 1-20), '[WCB] ERROR: wcb->begin() failed', no mesh join - while USB still "
      "answers; the real deviceId is written back over USB and a restart rejoins the mesh (NaviCore restarts twice)",
      needs=["navicore"], links=[], opt_in="navicore_identity")
def bad_device_id(bench):
    """The map's nc.cfg.clamps_mesh row: SET_CONFIG stores wcbNetwork.deviceId unchecked (rc_config.h:1771), and
    WCB_Client::begin() hard-fails an id outside 1-20 before it touches any per-board table (WCB_Client.cpp:52-66), so
    setup() carries on without the mesh (NaviCore.ino:4847-4855; loop() skips wcb->update() while !wcbReady, :5420). The
    droid then answers over USB only until someone writes the id back and restarts it. Attended only (D-NC14): if the
    restore did not take, NaviCore stays off the mesh until `python -m hil.ncflash status` shows it answering and its
    deviceId is written back over USB. Only deviceId is sent: the password, quantity and octets stay as they are."""
    nc = _nc(bench)
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        me = g.before["wcbNetwork"]["deviceId"]
        failure = None
        try:
            g.nc.set_config({"wcbNetwork": {"deviceId": 21}})
            _, lines, _ = _restart(g.nc, "json")
            refused = any(re.search(r"device_id 21 is out of range", x) for x in lines)
            failed = any(x.startswith("[WCB] ERROR: wcb->begin() failed") for x in lines)
            joined = [x for x in lines if JOINED.match(x)]
            facts = dict(refused=refused, begin_failed=failed, joined=len(joined), pong=g.nc.ping())
            if not refused or not failed:
                problems.append(f"the boot on deviceId 21 printed WCB_Client's refusal {refused}, the begin() failure "
                                f"{failed}")
            if joined:
                problems.append("NaviCore still joined the mesh on deviceId 21")
        except Exception as e:  # noqa: BLE001 - raised by _put_back once the deviceId is back
            failure = e
        m2 = g.nc.dev.mark()
        _put_back(g.nc, g.before_text, failure)
        back = next((j.groups() for x in g.nc.dev.since(m2) for j in [JOINED.match(x)] if j), None)
        facts["rejoined"] = back
        if back is None or back[0] != str(me):
            problems.append(f"after the restore NaviCore joined as {back}, not device {me}")
    bench.note(f"deviceId 21: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ nothing fires at boot (the SBUS controller)
def _set_targets(data):
    """The Pololu-protocol setTarget frames in `data` (0xAA, device, 0x04, channel, low 7, high 7) -> [(device,
    channel, target)]. A frame's other bytes are all below 0x80, so 0xAA only ever starts one."""
    out, i = [], data.find(b"\xaa")
    while 0 <= i <= len(data) - 6:
        if data[i + 2] == MAESTRO_SET_TARGET:
            out.append((data[i + 1], data[i + 3], data[i + 4] | data[i + 5] << 7))
        i = data.find(b"\xaa", i + 1)
    return out


def _knob_stick(cfg, ncfg):
    """(axis, knob label, remote slots) for a controller stick (getcfg's lx/ly/rx/ry channels) bound to a NaviCore
    Maestro-passthrough knob whose outputs, in every mode, are all remote slots (type 2): its frames reach the W1S1 probe
    and no local servo moves. None when there is none."""
    maes = ncfg.get("maestros") or []
    remote = {i + 1 for i, s in enumerate(maes) if isinstance(s, dict) and s.get("type") == 2}
    for axis in ("ly", "lx", "ry", "rx"):
        ch = cfg.get(axis)
        for label, k in (ncfg.get("knobs") or {}).items():
            if not isinstance(k, dict) or k.get("function") != 1 or k.get("channel") != ch or not ch:
                continue
            targets = {o.get("target") for key in ("outputs", "outputs2", "outputs3") for o in (k.get(key) or [])}
            if targets and targets <= remote:
                return axis, label, sorted(targets)
    return None


def _switch_marker(bench, cfg, ncfg):
    """A NaviCore switch whose tier at another position than the current one broadcasts plain text onto a wired WCB
    port, and the controller switch on its channel -> dict(index, back, go, label, link, text), or None. Only switches
    that drive neither the mode, a knob's mode override, nor a Maestro's easing; the text is the tier's own
    ';W<n>;S<p><text>' (wcb_broadcast) or ';S<p><text>' to W<target> (wcb_unicast)."""
    mode_sw = (ncfg.get("funcBindings") or {}).get("mode")
    overrides = {k.get("modeSwitchOverride") for k in entries(ncfg.get("knobs"))}
    labels = list((ncfg.get("switches") or {}).keys())
    for idx, label in enumerate(labels):
        s = ncfg["switches"][label]
        ch, positions = s.get("channel"), s.get("positions", 3)
        if not ch or idx == mode_sw or idx in overrides:
            continue
        tiers = {p: s.get(f"p{p}") or [] for p in (0, 1, 2)}
        if any(a.get("type") == "maestro" for acts in tiers.values() for a in acts):
            continue
        k, sw = next(((k, x) for k, x in enumerate(cfg.get("sw") or []) if x.get("c") == ch), (None, None))
        if sw is None:
            continue
        values = sw.get("v") or []

        def navicore_pos(q, values=values, positions=positions, t=sw.get("t", 0)):
            v = values[0 if (t != 0 and q == 1) else q] if len(values) > q else None
            if v is None:
                return None
            if positions == 2:
                return 2 if v > 900 else 0                # readSwitchPos (NaviCore.ino:566-571)
            return 0 if v < 582 else (2 if v > 1401 else 1)
        back = sw.get("pos", 0)
        now = navicore_pos(back)
        for go in (0, 1, 2):
            p = navicore_pos(go)
            if p is None or p == now:
                continue
            for a in tiers.get(p, []):
                cmd = a.get("cmd") or ""
                hit = re.fullmatch(r";W(\d+);S([1-5])([A-Za-z0-9]+)", cmd) if a.get("type") == "wcb_broadcast" else \
                    re.fullmatch(r";S([1-5])([A-Za-z0-9]+)", cmd) if a.get("type") == "wcb_unicast" else None
                if not hit:
                    continue
                wcb, port, text = ((int(hit.group(1)), hit.group(2), hit.group(3)) if len(hit.groups()) == 3 else
                                   (int(a.get("target") or 0), hit.group(1), hit.group(2)))
                wire = bench.links.get(wcb, f"S{port}")
                if wire is not None:
                    return dict(index=k, back=back, go=go, label=label, link=wire, text=text)
    return None


@test("sbus.boot_quiet", "A restart with the controller held - a stick off-centre on a remote-only knob, a switch at a "
      "position whose tier broadcasts a marker, a matrix button pressed on an unmapped slot - fires nothing: no setTarget "
      "frame on W1S1, no marker, no rc_trig while held or after the release; the local Maestro's position and error "
      "register unchanged (NaviCore restarts; Maestro 2 moves with the stick)", needs=["sbus", "navicore", "wcb1"],
      opt_in="navicore_reboot")
def boot_quiet(bench):
    """The map's nc.safety.boot_quiet, nc.knob.boot_prime, nc.switch.seed_no_fire, nc.matrix.debounce_arm and
    nc.mae.boot_tx_high rows. At boot: a knob's first frame only seeds its baseline (knobPrimed, NaviCore.ino:2596,
    :2652-2655); a switch's first decoded position is seeded without firing its tier (g_switchSeedPending,
    :2428-2451), though its easing is applied (setSpeed/setAccel frames, :2466-2478, which this test allows: only
    setTarget moves a servo); the matrix needs a confirmed neutral before its first press (matrixArmed false, :453), so
    a button held through the boot fires nothing, not even on its release; the Maestro TX line is driven high before
    Serial2 opens (:4508-4509), so the local Maestro reads no garbage. Remote Maestro frames reach the W1S1 probe
    (NAVICORE.md §1.2); the stick is one whose knob drives only remote slots (J4 here: Maestro 2, a real servo, and
    nobody's devices 3-7). The stick is moved 3 s before the restart, so the knob's 1500 ms idle release has gone out
    before the window opens."""
    w1s1 = link(bench, 1, "S1")
    ctl, nc = SbusCtl(bench.dev("sbus")), _nc(bench)
    state = nc.sbus_full_rate()             # a stall just before (a config restore) reads low for a second
    if state["fps"] < SBUS_FULL_FPS or state["variant"] != "SBUS-24":
        raise Skip(f"NaviCore sees no full-rate SBUS-24 stream (fps {state['fps']}, {state['variant']})")
    _restartable(nc)
    cfg, ncfg = ctl.cfg(), nc.config()
    stick = _knob_stick(cfg, ncfg)
    if stick is None:
        raise Skip("no controller stick bound to a knob that drives only remote Maestro slots")
    mode = nc.mode()
    i, button, slot = matrix_button(nc, cfg, ncfg, mode)
    sw = _switch_marker(bench, cfg, ncfg)
    local = nc.local_slots(ncfg)
    problems, facts = [], {}
    maestro0 = {s: (nc.mae_get(s, 0), nc.mae_err(s)) for s, _ in local}
    with nc_guard(bench, nc=nc) as g:
        try:
            ctl.axes(**{stick[0]: 0.6})
            if sw:
                sm = sw["link"].mark()
                ctl.switch(sw["index"], sw["go"])
            ctl.button(i, True)
            time.sleep(3.0)
            if sw:
                facts["switch_fired_before"] = sw["link"].received(sm).count(sw["text"].encode())
            wm = w1s1.mark()
            sm2 = sw["link"].mark() if sw else None
            m, _, _ = _restart(g.nc, "json")
            time.sleep(3.0)
            held = g.nc.cli("#L12")
            frames = _set_targets(w1s1.received(wm))
            trig_held = g.nc.rc_events(m, btn=slot)
            ctl.button(i, False)
            time.sleep(ncfg.get("tapWindowMs", 500) / 1000 + 1.5)
            trig_after = g.nc.rc_events(m, btn=slot)
            marks = sw["link"].received(sm2).count(sw["text"].encode()) if sw else None
            facts.update(stick=stick[:2], button=slot, switch=sw["label"] if sw else None, settargets=frames[:6],
                         switch_markers=marks, held=[x.rstrip() for x in held if x.startswith("Mode=")][:1])
            if frames:
                problems.append(f"{len(frames)} setTarget frame(s) on W1S1 after the restart (device, ch, target): "
                                f"{frames[:4]}: the boot moved a servo")
            if marks:
                problems.append(f"switch {sw['label']}'s tier ran at boot: {marks} '{sw['text']}' on {sw['link'].key}")
            if trig_held or trig_after:
                problems.append(f"rc_trig for slot {slot}: {len(trig_held)} while held, {len(trig_after)} by "
                                f"{ncfg.get('tapWindowMs', 500) + 1500} ms after the release")
        finally:
            ctl.button(i, False)
            ctl.axes(0, 0, 0, 0)
            if sw:
                ctl.switch(sw["index"], sw["back"])
            time.sleep(2.5)
        maestro1 = {s: (g.nc.mae_get(s, 0), g.nc.mae_err(s)) for s, _ in local}
    for s, (p0, e0) in maestro0.items():
        p1, e1 = maestro1.get(s, (None, None))
        if not isinstance(p0, int):
            facts[f"maestro{s}"] = f"not compared: ?MAE,GET,{s},0 answered {p0!r}"
        elif p1 != p0 or e1 != 0:
            problems.append(f"local Maestro slot {s}: ch 0 at {p1} (was {p0}), error register {e1} (was {e0}, read "
                            f"and cleared before the restart)")
    if sw is None:
        facts["switch"] = "none: no switch tier broadcasts a marker onto a wired port"
    bench.note(f"boot with the controller held: {facts}")
    assert not problems, "; ".join(problems)
