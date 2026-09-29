"""NaviCore's firmware images and OTA (docs/hil_plan/NAVICORE.md NC-WP2 and NC-WP10, ids ncota.*).

NC-WP2, this week's image and the recovery ladder: ncota.image_identity (read only) finds the build the board runs by the
App SHA256 it prints (navicore_ota.h:238-263); ncota.recovery_hard_reset proves the ladder's second rung through the
real hil/ncflash.py recover() (opt-in navicore_reboot); ncota.recovery_esptool proves its esptool rungs (opt-in
navicore_esptool, off: a watched run only). The third NC-WP2 test, nccfg.usb_no_late_reply, is in s40.

NC-WP10, NaviCore's OTA surface (navicore_ota.h): the ?OTALOCAL parser and its answers with no session, NaviCore as the
target of W1's relay and as a relay to W2, all with nothing erased; then, opt-in, small erasing sessions
(navicore_ota_erase), the full image over USB (navicore_ota_full), the full image through W1's relay
(navicore_ota_relay_full, off: run once by hand), and W2's own image relayed through NaviCore (ota_full_wcb2, the key
its W1-relayed twin ota.relay_full_same_image_wcb2 already uses).

Rules every test here keeps:
- A BEGIN that passes NaviCore's guards erases part of its inactive slot (esp_ota_begin, navicore_ota.h:154). Outside
  the erase opt-ins a BEGIN goes to NaviCore only where a guard refuses it before the erase (:143-153): family 0
  (NaviCore is family 1), size 0, or the Next slot's size + 1 as STATUS printed it just before (_refused_begin). Never
  a family above 255: the uint8_t cast (:281) wraps 257 to 1.
- A BEGIN relayed to W2 names its family (_relay_begin). NaviCore's relay parser reads a missing family as 0 (:494),
  which W2, a classic ESP32 (family 0), would accept and erase its slot for. Outside ota_full_wcb2 it is family 1.
- NaviCore is flashed only through hil/ncflash.py (flash() or recover()), only with the bench image
  (ncflash.BENCH_IMAGE, docs/HIL_WEEK_DECISIONS.md D45), only while it runs that image, and the test ends with it
  running again, proven by the App SHA256 NaviCore prints. A test that cannot prove it fails with the commands that
  put the image back (ncflash.put_back()).
- Every test that restarts NaviCore skips while its recorder holds anything (NaviCore.restart_blocker, D33) and runs
  inside nc_guard, which puts back and proves the config, command library, clips, learned peers and mode.
- A failure message quotes only first lines through redact_text (_line1): an ExpectTimeout's tail can carry NaviCore's
  boot banner, whose SoftAP line names the AP.
"""
import base64
import os
import re
import time
from contextlib import contextmanager

from hil import ncflash
from hil.checkpoint import redact_text
from hil.nc_guard import nc_guard
from hil.navicore import NaviCore, parse_boot
from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import Console, config_guard
from suites.s20_ota import SLOT_SIZE as WCB_SLOT_SIZE
from suites.s20_ota import _crc, _crun, _relay, _session_id
from suites.s20_ota import _status as _wcb_status

STATUS_HEAD, STATUS_TAIL = "---------- OTA Status ----------", "--------------------------------"   # :253, :262
BEGIN_USAGE = "[OTA] BEGIN usage: ?OTALOCAL,BEGIN,<imageSize>,<family 0|1>"                      # :279
DATA_USAGE = "[OTA] DATA usage: ?OTALOCAL,DATA,<offset>,<base64>"                                # :297
SUB_LIST = "(use STATUS|BEGIN|DATA|END|ABORT)"                                                    # :334
NAVICORE_FAMILY, WCB_FAMILY = 1, 0      # otaLocalChipFamily (:109-117): the S3 is 1; a classic-ESP32 WCB is 0
RELAY_CHUNK = 192                       # OTA_ESPNOW_PAYLOAD (:59): firmware bytes per relayed DATA frame
LOCAL_CHUNK = ncflash.CHUNK             # OTA_LOCAL_MAX_CHUNK (:106): decoded bytes per ?OTALOCAL,DATA line
NAVICORE_ID = 20                        # wcbNetwork.deviceId on this bench (NAVICORE.md §1.4)
REAPER_S = ncflash.IDLE_S               # OTA_TIMEOUT_MS (:104): the idle reaper ends a session this long after a write
USB_RESET_CODE = 11                     # ESP_RST_USB (IDF esp_system.h), 'USB peripheral' (NaviCore.ino:4390-4437)
USB_RESET_RTC = (21, 22)                # RTC codes: USB UART / USB JTAG chip reset (:4417-4418)
# The bench WCB image W1 and W2 are flashed from (results/builds/FLASHED.md; s20 _image): the only image this suite
# ever relays to W2, and only when W2 runs exactly its version.
WCB_BENCH_IMAGE = os.path.join(ncflash.BUILDS, "wcb-esp32-meshq", "WCB.ino.bin")


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _line1(e):
    """The first line of an error, credentials hashed. An ExpectTimeout's next lines are the device's last lines, which
    around a restart are NaviCore's boot banner (its SoftAP line names the AP) or a config line."""
    text = str(e)
    return redact_text(text.splitlines()[0] if text else type(e).__name__)[:240]


def _ota(lines):
    """The OTA parser's own lines and NaviCore's 'Unknown command:' answer (NaviCore.ino:3814-3816), right-stripped."""
    return [x.rstrip() for x in lines if x.startswith(("[OTA", "Unknown command:"))]


def _cli(nc, cmd, timeout=4.0):
    """One console line, then NaviCore.cli's '#L12' flush -> _ota() of what it printed. NaviCore answers lines in order,
    so everything `cmd` prints comes before the flush's own 'Mode=' line."""
    return _ota(nc.cli(cmd, timeout=timeout))


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


def _status(nc):
    return ncflash.ota_status(nc)


def _idle(nc):
    """?OTALOCAL,STATUS -> the parsed block (ncflash.parse_ota_status); Skip when a session is open (another tool is
    flashing NaviCore, and a BEGIN here would supersede it, :141) or the table has no spare slot."""
    st = _status(nc)
    if st["active"]:
        raise Skip(f"NaviCore has an OTA session open ({st['session']['written']} / {st['session']['size']} B): "
                   f"something else is flashing it")
    if not st["next"]:
        raise Skip("NaviCore's partition table has no spare OTA slot")
    return st


def _restartable(nc):
    why = nc.restart_blocker()
    if why:
        raise Skip(why)


def _slot(s):
    return f"'{s['label']}' @0x{s['addr']:06x}" if s else "none"


def _line_time(dev, since, rx):
    """The host time of the first line after mark `since` that matches `rx`, or None."""
    pat = re.compile(rx)
    for t, x in list(dev.lines[since:]):
        if pat.search(x):
            return t
    return None


def _refused_begin(size, family, nxt):
    """A ?OTALOCAL,BEGIN line that NaviCore's guards refuse before esp_ota_begin erases anything (navicore_ota.h:143-153):
    ValueError for any other. `nxt` is STATUS's Next slot."""
    if family == NAVICORE_FAMILY and 0 < size <= nxt["size"]:
        raise ValueError(f"BEGIN,{size},{family} would pass NaviCore's guards and erase its {_slot(nxt)} slot")
    if not 0 <= family <= 255:
        raise ValueError(f"family {family} does not survive the uint8_t cast (navicore_ota.h:281)")
    return f"?OTALOCAL,BEGIN,{size},{family}"


def _relay_begin(target, session, size, family):
    """A relayed ?OTA,BEGIN line, family always named: NaviCore's relay parser reads a missing one as 0
    (navicore_ota.h:494), and a WCB accepts 0 and erases its inactive slot."""
    if family not in (NAVICORE_FAMILY, WCB_FAMILY):
        raise ValueError(f"family {family}: 0 (a classic-ESP32 WCB) or 1 (an ESP32-S3)")
    return f"?OTA,BEGIN,{target},{session},{size},{family}"


def _bench_image():
    """check_image of results/builds/<BENCH_IMAGE> -> its info (without 'data'); Skip when the folder is not on this PC.
    An image that fails the check fails the test: it is the image every flash here writes."""
    folder = os.path.join(ncflash.BUILDS, ncflash.BENCH_IMAGE)
    if not os.path.isfile(os.path.join(folder, ncflash.IMAGE)):
        raise Skip(f"no bench image on this PC: results/builds/{ncflash.BENCH_IMAGE} holds no {ncflash.IMAGE}")
    info = ncflash.check_image(folder)
    info.pop("data", None)
    return info


def _runs(st, info):
    """True when STATUS `st` shows the image `info` running: its App SHA256 is the start of the image's ELF SHA-256."""
    return bool(st.get("app_sha")) and info["elf_sha"].startswith(st["app_sha"])


def _on_bench_image(nc, info):
    """STATUS, or Skip unless NaviCore runs the bench image: a same-image flash of anything else would change it."""
    st = _status(nc)
    if not _runs(st, info):
        raise Skip(f"NaviCore runs App SHA256 {st.get('app_sha')}, not the bench image {ncflash.BENCH_IMAGE} "
                   f"({info['elf_sha'][:16]}): flashing it would change NaviCore's firmware (see ncota.image_identity)")
    return st


def _ladder(nc):
    """The ladder's first two rungs (no esptool) after a flash that did not come back -> one line on how it ended."""
    try:
        r = ncflash.recover(nc)
        return f"the ladder brought NaviCore back at rung {r['rung']} ({r['version']})"
    except ncflash.RecoveryFailed as e:
        return f"the ladder did not bring it back: {_line1(e)}"


def _record(info, how, result, what):
    """A FLASHED.md row for a flash hil/ncflash.py did not stream itself (the relayed ones)."""
    manifest = info.get("manifest")
    ncflash.record_flash(ncflash.BUILDS, folder=info["folder"], elf_sha=info["elf_sha"],
                         tree=ncflash.tree_line((manifest or {}).get("navicore")), how=how, result=result,
                         what="; ".join(x for x in (what, ncflash.build_line(manifest)) if x))


class _Withheld:
    """NaviCore as hil/ncflash.py recover() sees it, with its PING answers withheld, so the ladder climbs past rung 1 on a
    healthy board exactly as it would for a hung app (the stubbing D35 did by hand). `until` says what ends it:
    'reset' - the rung-2 reset is real, and PING answers once it has been sent; 'esptool' - rung 2 is withheld too (no
    reset pulse, its wait fails), and PING answers once an esptool write-flash has succeeded (pass `esptool` below to
    recover()). Everything else is the real driver's."""

    def __init__(self, nc, until):
        if until not in ("reset", "esptool"):
            raise ValueError(until)
        self.nc, self.dev, self.until, self.withheld = nc, nc.dev, until, True

    def __getattr__(self, name):
        return getattr(self.nc, name)

    def ping(self):
        if self.withheld:
            raise AssertionError(f"{self.dev.name}: PING withheld by the test (the ladder's first rung is stubbed)")
        return self.nc.ping()

    def hard_reset(self, hold_s=0.2):
        if self.until == "reset":
            self.withheld = False
            return self.nc.hard_reset(hold_s)
        return self.dev.mark()                  # rung 2 stubbed: no pulse goes out

    def wait_boot(self, since=None, timeout=20.0):
        if self.withheld:
            raise AssertionError(f"{self.dev.name}: the ladder's second rung is stubbed by the test")
        return self.nc.wait_boot(since=since, timeout=timeout)

    def esptool(self, argv):
        rc, out = ncflash.run_esptool(argv)
        if rc == 0 and "write-flash" in [a.lower() for a in argv]:
            self.withheld = False
        return rc, out


@contextmanager
def _quiet_relay(dev):
    """session.log gets '?OTA,DATA,<t>,<s>,<off>:<crc>,<N base64 chars>' instead of each frame's base64 (a pass is ~6000
    frames), and an empty send is labelled; received lines are logged as always (ncflash._quiet_data's rule)."""
    log = getattr(dev, "log", None)
    if not log:
        yield
        return

    def filtered(name, direction, text):
        if direction == ">" and text.startswith("?OTA,DATA,"):
            head, _, b64 = text.rpartition(",")
            text = f"{head},<{len(b64)} base64 chars>"
        elif direction == ">" and text == "":
            text = "(empty line: output nudge)"
        log(name, direction, text)
    dev.log = filtered
    try:
        yield
    finally:
        dev.log = log


def _relay_stream(dev, data, target, session, family, *, nudge, begin_timeout=40.0, frame_timeout=4.0, max_stalls=60,
                  note=None):
    """Stream `data` to board `target` through the ?OTA relay on console `dev` (W1's, or NaviCore's), one 192-byte frame
    in flight, as s20's ota.relay_full_same_image_wcb2 and the Wizard do -> {'frames', 'end', 'secs'}. BEGIN must be
    ACKed OK at 0 (the target erases first: begin_timeout). Each DATA frame carries its CRC32 over '<offset>,<b64>'
    (navicore_ota.h:509-531; WCB_OTA.cpp) and is answered [OTA:ACK,<target>,<session>,<cursor>,<status>]: a cursor past
    the frame moves on; the frame's own cursor, or no answer (a frame or an ACK lost), sends it again after a growing
    pause; status ERR, or an OK cursor of 0 after the first frame (the session gone), fails at once. END's ACK is OK 0,
    or none when the target restarts before it is relayed. `nudge`: a lone newline every 0.25 s while waiting, for
    NaviCore's USB, which can hold a finished line (hil/ncflash.py). AssertionError on any failure; the caller aborts."""
    ack = re.compile(rf"^\[OTA:ACK,{target},{session},(\d+),(\d+)\]$")
    nudge_s = 0.25 if nudge else 0.0

    def exchange(cmd, timeout):
        m = dev.mark()
        dev.send(cmd)
        got = ncflash._await(dev, m, ack, timeout, nudge_s)
        return (int(got.group(1)), int(got.group(2))) if got else None
    t0 = time.monotonic()
    first = exchange(_relay_begin(target, session, len(data), family), begin_timeout)
    if first != (0, 0):
        raise AssertionError(f"the relayed BEGIN to WCB{target} was answered {first}, not OK at 0 (refused, or lost)")
    cursor, stalls, frames = 0, 0, 0
    step = max(len(data) // 10, 1)
    next_note = step
    while cursor < len(data):
        piece = _b64(data[cursor:cursor + RELAY_CHUNK])
        got = exchange(f"?OTA,DATA,{target},{session},{cursor}:{_crc(f'{cursor},{piece}')},{piece}", frame_timeout)
        frames += 1
        if got is not None and (got[1] != 0 or (got[0] == 0 and cursor >= RELAY_CHUNK)):
            raise AssertionError(f"WCB{target}'s session failed at offset {cursor} of {len(data)} (ACK {got[0]}, "
                                 f"{'ERR' if got[1] else 'OK'})")
        if got is None or got[0] <= cursor:
            stalls += 1
            if stalls > max_stalls:
                raise AssertionError(f"the relay stalled at offset {cursor} of {len(data)} ({max_stalls} frames with "
                                     f"no progress)")
            time.sleep(min(stalls * 0.05, 0.6))
            continue
        stalls, cursor = 0, got[0]
        if note and (cursor >= next_note or cursor == len(data)):
            next_note = cursor + step
            note(f"relay OTA to WCB{target}: {cursor} / {len(data)} B ({cursor * 100 // len(data)}%)")
    end = exchange(f"?OTA,END,{target},{session}", 15.0)
    if end not in (None, (0, 0)):
        raise AssertionError(f"END to WCB{target} was answered {end}: the image was refused")
    return {"frames": frames, "end": end, "secs": round(time.monotonic() - t0, 1)}


# ============================================================ NC-WP2: the image and the recovery ladder
@test("ncota.image_identity", "The App SHA256 NaviCore prints names exactly one build under results/builds - the week's "
      "bench image - whose ELF is beside it; PONG and STATUS carry that image's version; FLASHED.md's last write is "
      "that image (read only)", needs=["navicore"], links=[])
def image_identity(bench):
    """INF9 (a): FW_VERSION changes only on a NaviCore commit, so the version string cannot tell two builds apart; the ELF
    SHA-256 elf2image stamps into the image at 0xB0 can, and NaviCore prints its first 8 bytes as 'App SHA256:' in
    ?OTALOCAL,STATUS and the boot banner (navicore_ota.h:238-263, NaviCore.ino:4558). The build folder holding it must
    also hold the ELF that hashes to it (check_image), or a crash on this board cannot be decoded (addr2line against
    any other build gives plausible wrong names). The board must run ncflash.BENCH_IMAGE (D45), and FLASHED.md's newest
    row that put an image on the board (ncflash.last_written) must name it: anything else means NaviCore runs an image
    the harness did not record, and the message says how to put the bench image back."""
    nc = _nc(bench)
    version = nc.ping()
    st = _status(nc)
    sha = st["app_sha"]
    assert sha, ("?OTALOCAL,STATUS prints no App SHA256 line: this image predates INF9 (a), and nothing tells two builds "
                 f"of one commit apart; {ncflash.put_back()}")
    assert re.fullmatch(r"[0-9a-f]{16}", sha), f"App SHA256 {sha!r} is not 16 hex digits (navicore_ota.h otaAppSha16)"
    found = ncflash.builds_with_sha(sha)
    names = [os.path.basename(f) for f in found]
    assert found, (f"NaviCore runs App SHA256 {sha}, which no folder under results/builds holds: its crashes cannot be "
                   f"decoded and it cannot be flashed again; {ncflash.put_back()}")
    problems = []
    try:
        info = ncflash.check_image(found[0])
    except ncflash.ImageError as e:
        raise AssertionError(f"results/builds/{names[0]} holds the running image but fails check_image: "
                             f"{_line1(e)}") from None
    if not info["elf_checked"]:
        problems.append(f"results/builds/{names[0]} has no ELF beside its image: a crash on this board cannot be decoded")
    for label, got in (("PONG", version), ("STATUS Firmware", st["firmware"])):
        if got != info["version"]:
            problems.append(f"{label} reports {got}; the image in results/builds/{names[0]} carries {info['version']}")
    row = ncflash.last_written()
    if row is None:
        bench.note("FLASHED.md has no 'NaviCore flashes' row that put an image on the board: not compared")
    elif not sha.startswith(row["sha"][:16].lower()) or row["folder"] not in names:
        problems.append(f"FLASHED.md's newest write is results/builds/{row['folder']} ({row['sha']}, {row['when']}, "
                        f"{row['how']}), but NaviCore runs {', '.join(names)} ({sha}): a write the harness did not record")
    if ncflash.BENCH_IMAGE not in names:
        problems.append(f"NaviCore runs {', '.join(names)}, not this week's bench image {ncflash.BENCH_IMAGE} (D45); "
                        f"{ncflash.put_back()}")
    bench.note(f"NaviCore runs {_slot(st['running'])}, App SHA256 {sha} = results/builds/{', '.join(names)} "
               f"({info['version']}, ELF {'beside it' if info['elf_checked'] else 'MISSING'}); FLASHED.md's last "
               f"write: {row['folder'] + ' ' + row['when'] if row else 'none recorded'}")
    assert not problems, "; ".join(problems)


@test("ncota.recovery_hard_reset", "The recovery ladder's second rung through the real hil/ncflash.py recover(), its PING "
      "rung made to fail: the USB-Serial/JTAG reset restarts NaviCore (its uptime starts again; the banner, when "
      "captured, names reset 11, the USB peripheral) and PING answers the same version on the same slot and App SHA256; "
      "config, command library, clips and learned peers unchanged (NaviCore restarts)",
      needs=["navicore"], links=[], opt_in="navicore_reboot")
def recovery_hard_reset(bench):
    """INF4's ladder (hil/ncflash.py recover; docs/HIL_TESTING.md §5). Rung 1 PINGs; rung 2 is NaviCore.hard_reset, RTS=1
    with DTR=0 and a DTR write so usbser.sys sends the line state (serialdev.usb_jtag_reset), then wait_boot and PING. A
    healthy board answers rung 1, so the test withholds PING until the reset has gone out (_Withheld): the ladder then
    takes rung 2 exactly as it would for a hung app, and stops there (allow_esptool=False). That the chip really reset
    is shown by its uptime: GET_MESH_STATS' upMs is millis() (rc_telemetry.h:1371-1378), and it must now be below the
    time since the reset. The banner's reason is 11, ESP_RST_USB, with RTC code 21 or 22 (NaviCore.ino:4390-4437), as
    `python -m hil.ncflash reset` printed it on the bench on 2026-09-28 (results/builds/ncflash-logs); it is checked
    when the banner arrives, and its absence is noted. A chip reset boots the app: the strap is not re-sampled into
    download mode, and the bootloader is untouched. If rung 2 fails, the message names the esptool rungs."""
    nc = _nc(bench)
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        version = g.nc.ping()
        st0 = _status(g.nc)
        up0 = g.nc.mesh_stats()["upMs"]
        m = g.nc.dev.mark()
        t0 = time.monotonic()
        try:
            r = ncflash.recover(_Withheld(g.nc, "reset"), allow_esptool=False, boot_timeout=30)
        except ncflash.RecoveryFailed as e:
            raise AssertionError(f"rung 2 did not bring NaviCore back: {_line1(e)}; {ncflash.put_back()}") from None
        up1 = g.nc.mesh_stats()["upMs"]
        since_ms = (time.monotonic() - t0) * 1000
        lines = g.nc.dev.since(m)
        boot = parse_boot(lines)
        st1, v1 = _status(g.nc), g.nc.ping()
        facts = dict(rung=r["rung"], up0=up0, up1=up1, since_ms=round(since_ms), reset=boot["reset_code"],
                     rtc=boot["rtc"], reopened=any(x.startswith("<<reopened") for x in lines))
    if r["rung"] != "hard_reset":
        problems.append(f"the ladder ended at rung {r['rung']!r}, not hard_reset ({'; '.join(r['tried'])})")
    if not up1 < since_ms - 100:        # without a restart the uptime is at least the time since the reset (s46)
        problems.append(f"NaviCore's uptime is {up1} ms, {since_ms:.0f} ms after the reset (it was {up0} ms before): "
                        f"the chip did not restart")
    if boot["download_mode"]:
        problems.append("the ROM printed 'waiting for download': the reset landed in download mode")
    if boot["reset_code"] is not None and (boot["reset_code"] != USB_RESET_CODE or boot["rtc"][0] not in USB_RESET_RTC):
        problems.append(f"the banner names reset {boot['reset_code']} ({boot['reset']}, RTC {boot['rtc']}), not "
                        f"{USB_RESET_CODE} (the USB peripheral, RTC 21/22)")
    if v1 != version:
        problems.append(f"PONG {v1} after the reset, {version} before")
    if st1["running"]["addr"] != st0["running"]["addr"] or st1["app_sha"] != st0["app_sha"]:
        problems.append(f"after the reset NaviCore runs {_slot(st1['running'])} App SHA256 {st1['app_sha']}, before "
                        f"{_slot(st0['running'])} {st0['app_sha']}")
    bench.note(f"rung 2: {facts}; banner {'seen' if boot['reset_code'] is not None else 'not captured'}")
    assert not problems, "; ".join(problems)


@test("ncota.recovery_esptool", "OPT-IN (navicore_esptool, watched): the ladder's esptool rungs through the real "
      "recover(), PING and the reset made to fail: the read-only download-mode probe finds none, esptool writes the "
      "bench image into app0 and boot_app0.bin into otadata in one connection, and NaviCore boots it: running app0, the "
      "bench image's App SHA256, the same version, a FLASHED.md row; config, command library, clips and learned peers "
      "unchanged (NaviCore restarts through ROM download mode)", needs=["navicore"], links=[],
      opt_in="navicore_esptool")
def recovery_esptool(bench):
    """D35's proof, repeatable: rungs 3 and 4 of hil/ncflash.py recover(), which run only with allow_esptool=True. The
    probe (kick_argv: --before no-reset --after watchdog-reset chip-id) connects only to a chip already in ROM download
    mode and writes nothing; the write (write_argv: --before usb-reset --after watchdog-reset write-flash 0x10000 <app>
    0xe000 boot_app0.bin) is held to those two addresses by guard_writes, so the bootloader, the partition table, NVS,
    the config and the clips are never touched. The known-good image given is the bench image the board already runs
    (D45), so NaviCore ends as it began, now on app0 (boot_app0.bin selects it). The harness's port is closed while
    esptool holds it and reopened after. If the stubbed ladder fails, the real one runs once (nothing withheld, esptool
    allowed; its kick rung leaves download mode) before the test fails with the commands that put the bench image back.
    Watched runs only: nobody can replug a board this week (NAVICORE.md §6.2)."""
    nc = _nc(bench)
    info = _bench_image()
    _restartable(nc)
    problems = []
    with nc_guard(bench, nc=nc) as g:
        version = g.nc.ping()
        _on_bench_image(g.nc, info)
        rows0 = len(ncflash.flash_rows())
        stub = _Withheld(g.nc, "esptool")
        try:
            r = ncflash.recover(stub, known_good=info["folder"], allow_esptool=True, esptool=stub.esptool,
                                boot_timeout=45, what="ncota.recovery_esptool")
        except ncflash.RecoveryFailed as e:
            first = _line1(e)
            try:
                r2 = ncflash.recover(g.nc, known_good=info["folder"], allow_esptool=True, boot_timeout=45,
                                     what="ncota.recovery_esptool, the real ladder after the stubbed one failed")
                again = f"the real ladder then brought it back at rung {r2['rung']}"
            except ncflash.RecoveryFailed as e2:
                again = f"the real ladder failed too: {_line1(e2)}"
            raise AssertionError(f"the esptool rungs did not bring NaviCore back: {first}; {again}; "
                                 f"{ncflash.put_back()}") from None
        st1, v1 = _status(g.nc), g.nc.ping()
        rows = ncflash.flash_rows()
    if r["rung"] != "esptool":
        problems.append(f"the ladder ended at rung {r['rung']!r}, not esptool ({'; '.join(r['tried'])})")
    if not _runs(st1, info) or st1["running"]["label"] != "app0":
        problems.append(f"NaviCore runs {_slot(st1['running'])} App SHA256 {st1['app_sha']}, not the bench image in "
                        f"app0 ({info['elf_sha'][:16]}); {ncflash.put_back()}")
    if v1 != version:
        problems.append(f"PONG {v1} after the esptool write, {version} before")
    new = rows[rows0:]
    if [(x["folder"], x["how"], x["result"]) for x in new] != [(ncflash.BENCH_IMAGE, "esptool app0 + otadata",
                                                                "written (recovery)")]:
        problems.append(f"FLASHED.md gained {[(x['folder'], x['how'], x['result']) for x in new]}, not one esptool row "
                        f"for {ncflash.BENCH_IMAGE}")
    bench.note(f"esptool rungs: {'; '.join(r['tried'])}; NaviCore on {_slot(st1['running'])} {st1['app_sha']}")
    assert not problems, "; ".join(problems)


# ============================================================ NC-WP10: the surface, nothing erased
@test("ncota.local_status_parse", "?OTALOCAL,STATUS prints eight lines in order: chip ESP32-S3 family 1, PONG's firmware, "
      "a 16-hex App SHA256, the running and next slots at the partition table's app0/app1 addresses and size, session "
      "idle; ',', a lower-case and a padded STATUS print the same; a bare ?OTALOCAL and a lower-case prefix are "
      "'Unknown command' (read only)", needs=["navicore"], links=[])
def local_status_parse(bench):
    """otaPrintStatus (navicore_ota.h:250-263) and processOtaLocalCommand's STATUS branch (:269-275): the sub-command is
    trimmed and upper-cased, and an empty one is STATUS. execCliLine matches the '?OTALOCAL,' prefix case-sensitively and
    with its comma (NaviCore.ino:3372), so '?OTALOCAL' and '?otalocal,status' fall through to 'Unknown command: <line>'
    (:3814-3816), unlike a WCB, where both print STATUS (ota.local_parse_help_unknown). The slots are checked against
    the partition table the bench image was built with (partitions.csv: app0 at 0x10000 and app1 at 0x1F0000, 0x1E0000
    each)."""
    nc = _nc(bench)
    version = nc.ping()
    text, _ = ncflash._partition_csv(os.path.join(ncflash.BUILDS, ncflash.BENCH_IMAGE))
    if not text:
        raise Skip("no NaviCore partitions.csv here to check the slots against")
    slots = {name: (p["offset"], p["size"]) for name, p in ncflash.partitions(text).items()
             if p["type"] == "app" and p["subtype"] in ("ota_0", "ota_1")}

    def block(cmd):
        lines = [x.rstrip() for x in nc.cli(cmd)]
        heads = [i for i, x in enumerate(lines) if x == STATUS_HEAD]
        return lines[heads[-1]:heads[-1] + 8] if heads else []
    want = [("the heading", re.escape(STATUS_HEAD)), ("Chip", r"Chip:        ESP32-S3 \(family 1\)"),
            ("Firmware", rf"Firmware:    {re.escape(version)}"), ("App SHA256", r"App SHA256:  [0-9a-f]{16}"),
            ("Running", r"Running:     '(app[01])' @0x([0-9a-f]{6}) \((\d+) B\)"),
            ("Next", r"Next \(OTA\):  '(app[01])' @0x([0-9a-f]{6}) \((\d+) B\)"), ("Session", r"Session:     idle"),
            ("the closing rule", re.escape(STATUS_TAIL))]
    got = block("?OTALOCAL,STATUS")
    problems = [f"line {i + 1} ({label}): {got[i] if i < len(got) else 'missing'!r}"
                for i, (label, rx) in enumerate(want) if i >= len(got) or not re.fullmatch(rx, got[i])]
    if len(got) == 8 and not problems:
        run = re.fullmatch(want[4][1], got[4])
        nxt = re.fullmatch(want[5][1], got[5])
        for label, m in (("Running", run), ("Next", nxt)):
            name, addr, size = m.group(1), int(m.group(2), 16), int(m.group(3))
            if slots.get(name) != (addr, size):
                problems.append(f"{label} names {name} at 0x{addr:06x} ({size} B); partitions.csv has "
                                f"{slots.get(name)}")
        if run.group(1) == nxt.group(1):
            problems.append(f"Running and Next are both {run.group(1)}")
    for cmd in ("?OTALOCAL,", "?OTALOCAL,status", "?OTALOCAL, Status "):
        again = block(cmd)
        if again != got:
            problems.append(f"{cmd!r} printed {len(again)} STATUS lines that differ from ?OTALOCAL,STATUS's")
    for cmd in ("?OTALOCAL", "?otalocal,status"):
        answer = _cli(nc, cmd)
        if answer != [f"Unknown command: {cmd}"]:
            problems.append(f"{cmd!r} answered {answer}, not 'Unknown command: {cmd}'")
    bench.note("STATUS: " + " | ".join(got[1:7]))
    assert not problems, "; ".join(problems)


@test("ncota.local_nosession_errors", "With no session: DATA NAKs at cursor 0, END errs, ABORT is silent; the BEGIN and "
      "DATA usage lines; base64 errors -44 (a bad character) and -42 (over 1024 decoded bytes) with no marker; BAUD and "
      "an unknown sub-command answer the sub-command list; BEGIN's guards refuse family 0, size 0 and the Next slot's "
      "size + 1 before any erase; the session stays idle and the running slot unchanged (read only)",
      needs=["navicore"], links=[])
def local_nosession_errors(bench):
    """processOtaLocalCommand (navicore_ota.h:269-335) with no session open. NaviCore's local OTA has no BAUD
    sub-command (the WCB's has, ota.local_baud_*), so '?OTALOCAL,BAUD,921600' is an unknown one here. A DATA line whose
    base64 does not decode prints its error and no [OTA:...] marker at all (:303-306), which is why hil/ncflash.py
    settles a silent chunk with STATUS. The three BEGINs are refused by otaBegin's guards (:143-153), which run before
    esp_ota_begin erases anything (:154); the oversize one is the Next slot's size + 1 from the STATUS read just before
    (_refused_begin refuses to build any other). Each BEGIN prints '[OTA:BEGIN,START]' before its guard runs
    (:288-291)."""
    nc = _nc(bench)
    st0 = _idle(nc)
    nxt = st0["next"]
    big = _b64(bytes(LOCAL_CHUNK + 1))           # 1368 characters that decode to 1025 bytes: one over the buffer (:267)

    def refused(why):
        return ["[OTA:BEGIN,START]", why, "[OTA:BEGIN,ERR,0]"]
    too_big = f"[OTA] BEGIN rejected: image {{}} B exceeds partition '{nxt['label']}' ({nxt['size']} B)"
    checks = [("?OTALOCAL,DATA,0,AAAA", ["[OTA] DATA rejected at offset 0 (write cursor at 0)", "[OTA:NAK,0]"]),
              ("?OTALOCAL,END", ["[OTA] END: no matching active session", "[OTA:END,ERR]"]),
              ("?OTALOCAL,ABORT", []),
              ("?OTALOCAL,BEGIN,4096", [BEGIN_USAGE]),
              ("?OTALOCAL,BEGIN", [BEGIN_USAGE]),
              ("?OTALOCAL,DATA,0", [DATA_USAGE]),
              ("?OTALOCAL,DATA,0,@@@@", [f"[OTA] DATA base64 error -44 (chunk too big? max {LOCAL_CHUNK} B decoded)"]),
              (f"?OTALOCAL,DATA,0,{big}", [f"[OTA] DATA base64 error -42 (chunk too big? max {LOCAL_CHUNK} B decoded)"]),
              ("?OTALOCAL,BAUD,921600", [f"[OTA] unknown subcommand 'BAUD' {SUB_LIST}"]),
              ("?OTALOCAL,foo", [f"[OTA] unknown subcommand 'FOO' {SUB_LIST}"]),
              (_refused_begin(4096, WCB_FAMILY, nxt),
               refused(f"[OTA] BEGIN rejected: image chip family {WCB_FAMILY} != this board 1 (brick guard)")),
              (_refused_begin(0, NAVICORE_FAMILY, nxt), refused(too_big.format(0))),
              (_refused_begin(nxt["size"] + 1, NAVICORE_FAMILY, nxt), refused(too_big.format(nxt["size"] + 1)))]
    problems = []
    try:
        for cmd, want in checks:
            got = _cli(nc, cmd, timeout=6)
            if got != want:
                problems.append(f"{cmd[:40]}: {got}, expected {want}")
    finally:
        st1 = _status(nc)
    if st1["active"]:
        problems.append(f"a session is open afterwards ({st1['session']})")
    if st1["running"]["addr"] != st0["running"]["addr"]:
        problems.append(f"NaviCore now runs {_slot(st1['running'])}, before {_slot(st0['running'])}")
    assert not problems, "; ".join(problems)


@test("ncota.relay_target_nosession", "W1 relays DATA, END and ABORT to NaviCore (20) with no session open: DATA is ACKed "
      "ERR at cursor 0, END ERR 0 with NaviCore's 'END: no matching active session', ABORT OK 0; nothing written or "
      "erased, NaviCore's session idle and its running slot unchanged", needs=["navicore", "wcb1"], links=[])
def relay_target_nosession(bench):
    """NaviCore's target side (navicore_ota.h:372-448), fed by W1's ?OTA relay (WCB_OTA.cpp), queued off the receive
    callback and run from loop() (:556-590). With no session: DATA's otaWrite refuses, and the ACK reports the session's
    real state, ERR at cursor 0 (:397-411); END's otaEnd prints its refusal and ACKs ERR 0 (:414-428); ABORT tears down
    nothing, prints nothing and ACKs OK 0 (:442-448). The DATA is three zero bytes at offset 0, which even an open
    session would refuse for their missing 0xE9 magic, and STATUS is read first: nothing is relayed to a NaviCore with a
    session open. No BEGIN: ota.relay_navicore_brick_guard sends the one a guard refuses (family 0)."""
    nc, w = _nc(bench), WCB(bench.dev("wcb1"))
    st0 = _idle(nc)
    s = _session_id()
    nm = nc.dev.mark()
    acks, problems = {}, []
    for what, cmd, timeout in (("DATA", f"?OTA,DATA,{NAVICORE_ID},{s},0:{_crc('0,AAAA')},AAAA", 6),
                               ("END", f"?OTA,END,{NAVICORE_ID},{s}", 8),
                               ("ABORT", f"?OTA,ABORT,{NAVICORE_ID},{s}", 6)):
        try:
            acks[what] = _relay(w, cmd, NAVICORE_ID, s, timeout=timeout)
        except AssertionError:
            acks[what] = None
    lines = _flushed(nc, nm)
    st1 = _status(nc)
    want = {"DATA": (0, 1), "END": (0, 1), "ABORT": (0, 0)}
    if acks != want:
        problems.append(f"W1 relayed ACKs {acks} (offset, status), expected {want}")
    if "[OTA] END: no matching active session" not in lines:
        problems.append("NaviCore did not print '[OTA] END: no matching active session'")
    stray = [x for x in _ota(lines) if x.startswith(("[OTA] BEGIN", "[OTA] aborted", "[OTA] write", "[OTA] esp_ota"))]
    if stray:
        problems.append(f"NaviCore printed {stray}")
    if st1["active"] or st1["running"]["addr"] != st0["running"]["addr"]:
        problems.append(f"afterwards: session {'ACTIVE' if st1['active'] else 'idle'}, running {_slot(st1['running'])} "
                        f"(before {_slot(st0['running'])})")
    assert not problems, "; ".join(problems)


@test("ncota.navicore_as_relay_nonerasing", "NaviCore as a relay, nothing erased: its relay parser's refusals send "
      "nothing (bad target, sub-command, usage, a CRC mismatch dropped, base64 -42/-44); to W2 a family-1 BEGIN is "
      "refused by W2's brick guard ([OTA:ACK,2,s,0,1] on NaviCore's USB), DATA and END with no session are ACKed ERR 0, "
      "ABORT OK 0; W2's session stays idle and its running slot unchanged", needs=["navicore", "wcb1"], links=[])
def navicore_as_relay_nonerasing(bench):
    """processOtaRelayCommand (navicore_ota.h:475-554) builds each frame and unicasts it; the target's ACK comes back
    through the raw-packet hook and is printed from loop() as [OTA:ACK,<src>,<session>,<offset>,<status>]
    (handleOtaAckRelay :463-469). The CRC suffix guards the USB hop: a mismatch is dropped, with nothing sent
    (:519-531). W2 is a classic ESP32, family 0, so the family-1 BEGIN never reaches its esp_ota_begin
    (WCB_OTA.cpp otaBegin's guard); DATA, END and ABORT then find no session. W2's console (its own USB, or a relayed
    terminal) shows its side; its STATUS is read before and after."""
    nc = _nc(bench)
    s = _session_id()
    big = _b64(bytes(range(RELAY_CHUNK + 1)))                    # 193 bytes: one over the frame's buffer (:85, :537)
    parse = [("?OTA,BEGIN", "[OTA] relay: invalid target 0"),
             ("?OTA,DATA,21,1,0,AAAA", "[OTA] relay: invalid target 21"),
             ("?OTA,foo,2,1", "[OTA] relay: unknown subcommand 'FOO'"),
             ("?OTA,DATA,2,7", "[OTA] relay DATA: ?OTA,DATA,<t>,<s>,<offset>[:<crc32>],<b64>"),
             (f"?OTA,DATA,2,{s},0:DEADBEEF,AAAA",
              f"[OTA] relay DATA @0 DROPPED: crc {_crc('0,AAAA')} != DEADBEEF (b64 4 chars)"),
             (f"?OTA,DATA,2,{s},0:{_crc('0,' + big)},{big}", "[OTA] relay DATA base64 error -42"),
             (f"?OTA,DATA,2,{s},0:{_crc('0,@@@@')},@@@@", "[OTA] relay DATA base64 error -44")]
    problems = []
    nm = nc.dev.mark()
    for cmd, want in parse:
        got = _cli(nc, cmd)
        if want not in got:
            problems.append(f"{cmd[:36]}: {got}, expected {want!r}")
    time.sleep(2.0)
    sent = [x for x in _flushed(nc, nm) if x.startswith(f"[OTA:ACK,2,{s},")]
    if sent:
        problems.append(f"a refused relay line still reached W2: {sent}")
    ack = re.compile(rf"^\[OTA:ACK,2,{s},(\d+),(\d+)\]$")
    with Console(bench, 2) as c2:
        st2 = _wcb_status(_crun(c2, "?OTALOCAL,STATUS"))
        if st2["session"] != "idle":
            raise Skip(f"W2 has an OTA session open ({st2['session']})")
        cm = c2.mark()
        acks = {}
        for what, cmd, timeout in (("BEGIN", _relay_begin(2, s, 4096, NAVICORE_FAMILY), 10),
                                   ("DATA", f"?OTA,DATA,2,{s},0:{_crc('0,AAAA')},AAAA", 6),
                                   ("END", f"?OTA,END,2,{s}", 8), ("ABORT", f"?OTA,ABORT,2,{s}", 6)):
            m = nc.dev.mark()
            nc.dev.send(cmd)
            got = ncflash._await(nc.dev, m, ack, timeout, 0.25)
            acks[what] = (int(got.group(1)), int(got.group(2))) if got else None
        time.sleep(1.0)
        w2 = [x.rstrip() for x in c2.lines(cm)]
        st2b = _wcb_status(_crun(c2, "?OTALOCAL,STATUS"))
    want = {"BEGIN": (0, 1), "DATA": (0, 1), "END": (0, 1), "ABORT": (0, 0)}
    if acks != want:
        problems.append(f"relayed ACKs on NaviCore's USB {acks} (offset, status), expected {want}")
    for line in (f"[OTA] BEGIN rejected: image chip family {NAVICORE_FAMILY} != this board {WCB_FAMILY} (brick guard)",
                 "[OTA] END: no matching active session"):
        if not any(line in x for x in w2):
            problems.append(f"W2 did not print {line!r}")
    if st2b["session"] != "idle" or st2b["running"] != st2["running"]:
        problems.append(f"W2 afterwards: session {st2b['session']}, running {st2b['running']} (before {st2['running']})")
    assert not problems, "; ".join(problems)


# ============================================================ NC-WP10: opt-in, erasing and flashing
@test("ncota.local_begin_abort_timeout", "OPT-IN (navicore_ota_erase): a 4 KB ?OTALOCAL BEGIN opens session 1 on the Next "
      "slot (STATUS ACTIVE 0 / 4096) and ABORT ends it ('local abort command'); a second, fed only chunks NaviCore "
      "rejects at +10 s and +20 s, is ended by the idle reaper 30 s after its BEGIN ('session timed out'); the running "
      "slot and App SHA256 never change (~40 s)", needs=["navicore"], links=[], opt_in="navicore_ota_erase")
def local_begin_abort_timeout(bench):
    """otaBegin (navicore_ota.h:140-162) erases the first 4 KB of the inactive slot, the head of whatever image is there:
    on this bench the rollback image in app0 since D45, which is also the bootloader's fallback copy. `python -m
    hil.ncflash flash results/builds/navicore` still writes it back whenever it is wanted. checkOtaTimeout (:134-137,
    every loop()) ends a session 30 s after its last accepted write (OTA_TIMEOUT_MS :104). On the local path a rejected
    chunk does not refresh that (otaWrite refreshes only on a write, :176; the relayed DATA path has a keep-alive,
    :385-393), so the reaper fires 30 s after BEGIN. No chunk is ever accepted and END is never sent, so the boot slot
    cannot move. The waits nudge NaviCore's USB with a lone newline, which releases a held line (hil/ncflash.py)."""
    nc = _nc(bench)
    st0 = _idle(nc)
    nxt = st0["next"]
    begin_ok = f"[OTA] BEGIN ok: session 1, 4096 B -> partition '{nxt['label']}' @0x{nxt['addr']:06x} ({nxt['size']} B)"
    timed_out = r"^\[OTA\] aborted: session timed out \(current app intact\)$"
    problems, facts = [], {}

    def begin():
        m = nc.dev.mark()
        nc.dev.send("?OTALOCAL,BEGIN,4096,1")
        got = ncflash._await(nc.dev, m, ncflash.M_BEGIN_DONE, 40, 0.5)
        if not got or got.group(1) != "OK":
            raise AssertionError(f"a 4 KB BEGIN was not accepted: {got.group(0) if got else 'no answer in 40 s'} "
                                 f"({_ota(nc.dev.since(m))[:4]})")
        if begin_ok not in [x.rstrip() for x in nc.dev.since(m)]:
            problems.append(f"no {begin_ok!r} line")
        return m
    try:
        begin()
        st1 = _status(nc)
        if st1["session"] != {"id": 1, "written": 0, "size": 4096}:
            problems.append(f"STATUS after BEGIN shows session {st1['session']}, not ACTIVE id=1 0 / 4096")
        aborted = _cli(nc, "?OTALOCAL,ABORT")
        if aborted != ["[OTA] aborted: local abort command (current app intact)"]:
            problems.append(f"ABORT printed {aborted}")
        if _status(nc)["active"]:
            problems.append("a session is still open after ABORT")
        m = begin()
        t_begin = _line_time(nc.dev, m, r"^\[OTA:BEGIN,OK,0\]")
        naks = []
        for at in (REAPER_S / 3, REAPER_S * 2 / 3):
            time.sleep(max(0.0, t_begin + at - time.monotonic()))
            naks.append(_cli(nc, "?OTALOCAL,DATA,1024,AAAA"))
        got = ncflash._await(nc.dev, m, re.compile(timed_out), max(1.0, t_begin + REAPER_S + 15 - time.monotonic()),
                             0.5)
        t_out = _line_time(nc.dev, m, timed_out) if got else None
        facts = {"reaped_after_s": round(t_out - t_begin, 2) if t_out else None}
        for n, got_nak in enumerate(naks):
            if got_nak != ["[OTA] DATA rejected at offset 1024 (write cursor at 0)", "[OTA:NAK,0]"]:
                problems.append(f"the rejected chunk at +{REAPER_S * (n + 1) / 3:.0f} s printed {got_nak}")
        slack = max(0.5, REAPER_S / 12)          # 2.5 s at 30 s: loop() and USB latency, far below a refresh's +20 s
        if t_out is None:
            problems.append(f"the idle reaper never ended the second session ({REAPER_S + 15:.0f} s)")
        elif not REAPER_S - 0.5 <= t_out - t_begin <= REAPER_S + slack:
            problems.append(f"the reaper fired {t_out - t_begin:.1f} s after BEGIN, not {REAPER_S:.0f} s (OTA_TIMEOUT_MS): "
                            f"rejected chunks {'refreshed' if t_out - t_begin > REAPER_S else 'shortened'} the window")
    finally:
        nc.dev.send("?OTALOCAL,ABORT")          # silent when idle (:129-132)
        time.sleep(0.3)
    st2, v2 = _status(nc), nc.ping()
    if st2["active"] or st2["running"]["addr"] != st0["running"]["addr"] or st2["app_sha"] != st0["app_sha"]:
        problems.append(f"afterwards NaviCore runs {_slot(st2['running'])} {st2['app_sha']} with session "
                        f"{st2['session']}; before {_slot(st0['running'])} {st0['app_sha']}")
    bench.note(f"4 KB sessions on {_slot(nxt)}: {facts}; PONG {v2}")
    assert not problems, "; ".join(problems)


def _flash_pass(nc, info, what):
    """One hil/ncflash.py flash() of the bench image -> its result; AssertionError with where it stopped and, when the
    board did not come back or the running app is not proven intact, the ladder's outcome and the put-back commands."""
    try:
        return ncflash.flash(nc, info["folder"], what)
    except ncflash.NotBack as e:
        raise AssertionError(f"{what}: {_line1(e)}; {_ladder(nc)}; {ncflash.put_back()}") from None
    except ncflash.FlashError as e:
        tail = "the running app is intact" if e.intact else ncflash.put_back()
        raise AssertionError(f"{what}: {_line1(e)}; {tail}") from None


@test("ncota.local_full_same_image", "OPT-IN (navicore_ota_full): NaviCore re-flashed over USB (?OTALOCAL through "
      "hil/ncflash.py flash) with the bench image it runs, twice: each pass is verified at END and restarts into the "
      "other slot with the same App SHA256 and version, the second back on the slot it started on; config, command "
      "library, clips and learned peers unchanged (~3 min; NaviCore restarts twice)", needs=["navicore"], links=[],
      opt_in="navicore_ota_full")
def local_full_same_image(bench):
    """The config tool's 'Update over USB' path, driven by INF4's flash(): check_image, PING, STATUS, then 1024-byte
    chunks one at a time, END (esp_ota_end verifies the SHA-256 before the boot pointer moves, navicore_ota.h:206-224),
    the restart, and STATUS must show the other slot running with the image's Firmware and App SHA256. Only the bench
    image, only while NaviCore runs it, and two passes so the boot slot ends where it began, as the WCB twin
    (ota.local_full_same_image_wcb1) does. Both slots then hold the bench image: the rollback image D45 left in the
    other slot is gone from the board, and `python -m hil.ncflash flash results/builds/navicore` is still the
    one-command rollback. Each pass adds a FLASHED.md row. A pass that stops before END leaves the running app
    (FlashError says so); one that does not come back goes through the ladder's first two rungs before the test fails
    with the commands that put the bench image back."""
    nc = _nc(bench)
    info = _bench_image()
    _restartable(nc)
    passes = []
    with nc_guard(bench, nc=nc) as g:
        start = _on_bench_image(g.nc, info)
        _idle(g.nc)
        for n in (1, 2):
            passes.append(_flash_pass(g.nc, info, f"ncota.local_full_same_image pass {n}"))
        end = _status(g.nc)
    problems = []
    for n, p in enumerate(passes, 1):
        if not p["app_sha"]:
            problems.append(f"pass {n}: NaviCore printed no App SHA256 after the restart, so the image is not proven")
        if p["after"]["running"]["addr"] != p["before"]["next"]["addr"]:
            problems.append(f"pass {n}: back on {_slot(p['after']['running'])}, not {_slot(p['before']['next'])}")
    if end["running"]["addr"] != start["running"]["addr"] or not _runs(end, info):
        problems.append(f"NaviCore ends on {_slot(end['running'])} {end['app_sha']}, not {_slot(start['running'])} "
                        f"{info['elf_sha'][:16]}; {ncflash.put_back()}")
    bench.note("full OTA over USB: " + "; ".join(
        f"{p['secs']:.0f} s {_slot(p['before']['running'])} -> {_slot(p['after']['running'])}, {p['stats']['chunks']} "
        f"chunks, {p['stats']['naks']} NAK, {p['stats']['resyncs']} resync" for p in passes))
    assert not problems, "; ".join(problems)


@test("ncota.relay_full_via_w1", "OPT-IN (navicore_ota_relay_full, run by hand): W1 relays the bench image to NaviCore "
      "(?OTA, 192-byte frames over the mesh), twice: each pass is verified at END and NaviCore restarts into the other "
      "slot with the same App SHA256 and version, the second back on the slot it started on; config, command library, "
      "clips and learned peers unchanged (~12 min a pass; NaviCore restarts twice)", needs=["navicore", "wcb1"],
      links=[], opt_in="navicore_ota_relay_full")
def relay_full_via_w1(bench):
    """The Wizard's and the config tool's relayed update of a NaviCore, through a WCB (W1's ?OTA relay, WCB_OTA.cpp),
    end to end: NaviCore's target side (navicore_ota.h:372-440) erases the slot at BEGIN, writes each frame at its
    cursor and ACKs it, verifies at END, ACKs OK 0 four times and restarts. One frame in flight (_relay_stream); W1's
    USB at 115200 is the slow hop. Only the bench image, only while NaviCore runs it, two passes (the boot slot ends
    where it began); each pass adds a FLASHED.md row. A failed pass aborts the session from both sides (W1's relay
    and NaviCore's own ?OTALOCAL,ABORT: one session per board), so NaviCore keeps its running app; one that does not
    come back after END goes through the ladder's first two rungs before the test fails with the put-back commands."""
    nc, w = _nc(bench), WCB(bench.dev("wcb1"))
    info = _bench_image()
    with open(os.path.join(info["folder"], ncflash.IMAGE), "rb") as f:
        data = f.read()
    _restartable(nc)
    passes = []
    with nc_guard(bench, nc=nc) as g:
        start = _on_bench_image(g.nc, info)
        _idle(g.nc)
        for n in (1, 2):
            what = f"ncota.relay_full_via_w1 pass {n}"
            before = _status(g.nc)
            s = _session_id()
            nm = g.nc.dev.mark()
            try:
                with _quiet_relay(w.dev):
                    stats = _relay_stream(w.dev, data, NAVICORE_ID, s, NAVICORE_FAMILY, nudge=False, note=bench.note)
            except AssertionError as e:
                w.dev.send(f"?OTA,ABORT,{NAVICORE_ID},{s}")
                g.nc.dev.send("?OTALOCAL,ABORT")
                time.sleep(1.0)
                st = ncflash._status_or_none(g.nc)
                intact = bool(st) and st["running"]["addr"] == before["running"]["addr"]
                _record(info, "?OTA relay via W1", f"FAILED: {_line1(e)[:120]}; running app "
                        f"{'intact' if intact else 'NOT confirmed'}", what)
                raise AssertionError(f"{what}: {_line1(e)}; "
                                     f"{'the running app is intact' if intact else ncflash.put_back()}") from None
            try:
                g.nc.wait_boot(since=nm, timeout=45)
                after, version = _status(g.nc), g.nc.ping()
            except AssertionError as e:
                _record(info, "?OTA relay via W1", f"NOT BACK after END: {_line1(e)[:120]}", what)
                raise AssertionError(f"{what}: NaviCore took the image but did not come back ({_line1(e)}); "
                                     f"{_ladder(g.nc)}; {ncflash.put_back()}") from None
            ok = after["running"]["addr"] == before["next"]["addr"] and _runs(after, info) and \
                version == info["version"]
            _record(info, "?OTA relay via W1", (f"OK: {_slot(before['running'])} -> {_slot(after['running'])}, PONG "
                                                f"{version}, {stats['secs']:.0f} s, {stats['frames']} frames") if ok else
                    f"VERIFY FAILED after the restart: {_slot(after['running'])} {after['app_sha']} PONG {version}", what)
            passes.append(dict(before=before, after=after, version=version, ok=ok, **stats))
        end = _status(g.nc)
    problems = [f"pass {n}: NaviCore came back on {_slot(p['after']['running'])} App SHA256 {p['after']['app_sha']} "
                f"PONG {p['version']}, not the bench image on {_slot(p['before']['next'])}"
                for n, p in enumerate(passes, 1) if not p["ok"]]
    if end["running"]["addr"] != start["running"]["addr"] or not _runs(end, info):
        problems.append(f"NaviCore ends on {_slot(end['running'])} {end['app_sha']}, not {_slot(start['running'])}; "
                        f"{ncflash.put_back()}")
    bench.note("relay OTA to NaviCore via W1: " + "; ".join(
        f"{p['secs']:.0f} s, {p['frames']} frames, END ACK {p['end']}" for p in passes))
    assert not problems, "; ".join(problems)


def _wcb2_image(st):
    """The bench WCB image when it is the one W2 runs -> its bytes: WCB_BENCH_IMAGE, magic 0xE9, W2's STATUS firmware
    string in it, and room in W2's slot. Skip otherwise: relaying another image would change W2's firmware, and a CI
    release of the same version string is not the bench build (s20 _image)."""
    try:
        with open(WCB_BENCH_IMAGE, "rb") as f:
            data = f.read()
    except OSError:
        raise Skip("no bench WCB image on this PC (results/builds/wcb-esp32-meshq/WCB.ino.bin)") from None
    if data[:1] != b"\xe9" or st["firmware"].encode() not in data or len(data) > WCB_SLOT_SIZE:
        raise Skip(f"results/builds/wcb-esp32-meshq/WCB.ino.bin is not an image of W2's firmware {st['firmware']}: "
                   f"relaying it would change W2")
    return data


@test("ncota.relay_full_to_w2", "OPT-IN (ota_full_wcb2): NaviCore relays W2's own bench image to W2 (?OTA through "
      "NaviCore's USB and the mesh), twice: each pass is verified at END and W2 comes back on the other slot with the "
      "same firmware and config, the second on the slot it started on (slow: ~7200 frames a pass; W2 restarts twice)",
      needs=["navicore", "wcb1"], links=[], opt_in="ota_full_wcb2")
def relay_full_to_w2(bench):
    """NaviCore as the relay (processOtaRelayCommand, navicore_ota.h:475-554): the config tool's and the Wizard's path
    for updating a WCB through a NaviCore. The twin of ota.relay_full_same_image_wcb2, with NaviCore in W1's place:
    W2's ACKs come back through NaviCore's raw-packet hook and print from loop() (:463-469, :576-590), and NaviCore's
    USB is nudged while it waits. Only the bench build, and only when W2 runs its version (_wcb2_image). W2 sends its
    END ACK once, 300 ms before it restarts, so W1's '[ETM] WCB2 came ONLINE (boot)' is what shows the pass landed. A
    failed pass aborts W2's session through NaviCore: W2 keeps its running app. W2's own USB cable is the recovery if a
    new image did not run (s20)."""
    nc, w = _nc(bench), WCB(bench.dev("wcb1"))
    passes = []
    with config_guard(bench, 2), Console(bench, 2) as c2:
        first = _wcb_status(_crun(c2, "?OTALOCAL,STATUS"))
        if first["family"] != WCB_FAMILY or first["session"] != "idle":
            raise Skip(f"W2: family {first['family']}, session {first['session']}")
        data = _wcb2_image(first)
        for n in (1, 2):
            before = _wcb_status(_crun(c2, "?OTALOCAL,STATUS"))
            s = _session_id()
            wm = w.dev.mark()
            try:
                with _quiet_relay(nc.dev):
                    stats = _relay_stream(nc.dev, data, 2, s, WCB_FAMILY, nudge=True, note=bench.note)
                w.dev.expect(r"\[ETM\] WCB2 came ONLINE \(boot\)", timeout=30, since=wm)
            except AssertionError as e:
                nc.dev.send(f"?OTA,ABORT,2,{s}")
                time.sleep(1.0)
                raise AssertionError(f"pass {n}: {_line1(e)} (W2's session was aborted through NaviCore; W2 keeps "
                                     f"{before['running']} unless it restarted)") from None
            time.sleep(4)
            after = _wcb_status(_crun(c2, "?OTALOCAL,STATUS"))
            passes.append(dict(before=before, after=after, **stats))
    problems = []
    for n, p in enumerate(passes, 1):
        if p["after"]["running"] != p["before"]["next"] or p["after"]["firmware"] != p["before"]["firmware"]:
            problems.append(f"pass {n}: W2 runs {p['after']['running']} {p['after']['firmware']}, expected "
                            f"{p['before']['next']} {p['before']['firmware']}")
        if p["after"]["session"] != "idle":
            problems.append(f"pass {n}: W2's session is {p['after']['session']}")
    if passes and passes[-1]["after"]["running"] != passes[0]["before"]["running"]:
        problems.append("W2 did not end on its original slot")
    bench.note("relay OTA to W2 via NaviCore: " + "; ".join(
        f"{p['secs']:.0f} s, {p['frames']} frames, END ACK {p['end']}" for p in passes))
    assert not problems, "; ".join(problems)
