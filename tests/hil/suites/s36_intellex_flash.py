"""Flashing through Intellex (docs/hil_plan/INTELLEX.md IX-WP10; docs/HIL_TESTING.md §10).

W2, never another board: a classic ESP32 (HW 2.4, CP210x, COM15) whose ROM loader answers every auto-reset, running the
bench image the harness keeps in results/builds/wcb-esp32-meshq (its FLASHED.md; from a worktree, the main checkout's:
hil/intellex.py builds_dir). A test flashes that image back, so W2 ends on the image it started with (identity-
preserving): each test first reads W2's version and skips unless the image carries it - flashing anything else would
change W2 - and ends with the same version, W2 running app0 (Intellex writes the app at 0x10000 and erases otadata,
wcb_flash.py write_list), W1 having heard W2 boot (the flash really restarted it) and config_guard on every WCB.

The image reaches Intellex the way it would on a con floor: offline (INTELLEX_OFFLINE), from a firmware cache seeded into
the stage for the test branch hil-bench (seed_firmware: src/firmware/wcb/hil-bench/ with GitHub's listing shape), with
the files renamed to a release set - WCB_<version>_hilbench_ESP32.bin, _part.bin, _boot.bin - so the host's anchored
pattern (wcb_flash.py _app_re) takes the tag <version>_hilbench and pairs the three by it. The other product's branch is
hil-none, which nothing is cached for: no stray NaviCore flash could find an image to write. The host may open COM15
alone (INTELLEX_SERIAL_ALLOW); W1, the probes, NaviCore and the SBUS controller cannot be reached.

The flashes go through the Wizard as a user runs them: its auto-connect (slot 1, then filed under W2's number), the
shim's mesh routing through W2 (W1 pulled and its remote terminal armed through W2), then boardGo(2, {mode}) - the
function its Go button runs - which the shim's replacement flashFirmware sends to /_api/flash-wcb. The spec follows
/_api/flash-status and, once the Wizard has reconnected and pulled W2 again, asks the harness (hook flash_done) to judge
the host's flash log (flash_log_problems): the regions esptool wrote and verified, the offline cache, the mode's own
lines. A W2 that does not answer afterwards gets an EN reset, then the same image flashed again in full through
Intellex's own API (the plan's first recovery); the last resort is `arduino-cli upload`, which needs Greg.

NaviCore (intellex_flash_navicore) is flashed through its config tool's Update Firmware, app0 alone, from the bench
image it runs (results/builds/navicore-hil1), inside nc_guard, recovered by ncflash's ladder. The Factory Reset
(intellex_flash_factory, attended) erases W2's NVS and restores it from its own chain (suites/s31_password_erase.py).
Nothing here moves a servo but NaviCore's restart (hil/servos.py): a WCB restart writes nothing to a Maestro.
"""
import os
import re
import struct
import time

from hil import ncflash, optin
from hil.intellex import (IntellexHost, builds_dir, copy_logs, require, reset_into_app, run_intellex_test,
                          running_intellex, seed_firmware, stage)
from hil.navicore import NaviCore
from hil.nc_guard import nc_guard
from hil.runner import Skip, test
from hil.wcb import WCB
from hil.wizard import _reacquire
from suites.common import config_guard, token, usb_wcb
from suites.s20_ota import SLOT_SIZE, _local_status
from suites.s33_intellex_bench import _boot_edges
from suites.s34_intellex_tools import NAVICORE_ID, _nc_quiet
from suites.s35_intellex_wifi import _stop_rterm_all
from suites.s46_navicore_boot import _restartable, _restarted, _uptime_ms

BRANCH = "hil-bench"              # the test branch the cache is seeded under (fwcache: src/firmware/<product>/hil-bench)
NO_BRANCH = "hil-none"            # the other product's branch: nothing cached, so no flash of it can find an image
SETTINGS_W2 = {"branch": {"wcb": BRANCH, "navicore": NO_BRANCH}}
SETTINGS_NC = {"branch": {"navicore": BRANCH, "wcb": NO_BRANCH}}
W2_IMAGE = "wcb-esp32-meshq"      # results/builds/<this>: the image W1 and W2 run (its FLASHED.md)
W2_FILES = {"app": "WCB.ino.bin", "part": "WCB.ino.partitions.bin", "boot": "WCB.ino.bootloader.bin"}
# wcb_flash.py's flash map for a classic ESP32 (from flasher.js): boot, table, NVS, otadata, app0
BOOT, PART, NVS, OTADATA, APP = 0x1000, 0x8000, 0x9000, 0xE000, 0x10000
WROTE = re.compile(r"^Wrote \d+ bytes .*?\bat (0x[0-9a-fA-F]+)\b")          # esptool 5.3.1, once per region
VERIFIED = "Hash of data verified."                                          # esptool, after each region's write
FQBN_W2 = "esp32:esp32:esp32:PartitionScheme=min_spiffs"


# ------------------------------------------------------------------ the bench image and the seed (pure; selftest.py)
def partition_rows(table):
    """A partition table (ESP-IDF's binary, 32-byte entries starting 0xAA 0x50) -> [(type, subtype, offset, size, label)]
    up to the first entry that is not one (the MD5 row or 0xFF fill)."""
    rows = []
    for i in range(0, len(table) - 31, 32):
        e = table[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        typ, sub, off, size = e[2], e[3], struct.unpack_from("<I", e, 4)[0], struct.unpack_from("<I", e, 8)[0]
        rows.append((typ, sub, off, size, e[12:28].split(b"\0")[0].decode("ascii", "replace")))
    return rows


def w2_image_problems(files, version):
    """{"app", "part", "boot"} bytes of a WCB build against the version W2 reports -> [problem]: an app image (magic 0xE9)
    that carries the version and fits the min_spiffs app slot; a bootloader image; a partition table whose app0 is at
    0x10000 (where Intellex writes the app) and that has an otadata row at 0xE000."""
    out = []
    app, part, boot = files.get("app") or b"", files.get("part") or b"", files.get("boot") or b""
    if app[:1] != b"\xe9":
        out.append("the app is not an ESP32 image (no 0xE9 magic)")
    if version.encode() not in app:
        out.append(f"the app does not carry the version W2 reports ({version})")
    if len(app) > SLOT_SIZE:
        out.append(f"the app ({len(app)} B) does not fit the {SLOT_SIZE} B app slot")
    if boot[:1] != b"\xe9":
        out.append("the bootloader is not an ESP32 image (no 0xE9 magic)")
    rows = partition_rows(part)
    if not any(t == 0 and off == APP for t, _, off, _, _ in rows):
        out.append("the partition table has no app partition at 0x10000")
    if not any(t == 1 and s == 0 and off == OTADATA for t, s, off, _, _ in rows):
        out.append("the partition table has no otadata row at 0xE000")
    return out


def release_names(version, suffix="hilbench"):
    """The release names Intellex's WCB flasher pairs by one build tag (wcb_flash.py _app_re, fetch_images) -> (tag,
    {"app": name, "part": name, "boot": name}). The tag is what /_api/flash-status reports as the version flashed."""
    tag = f"{version}_{suffix}"
    return tag, {"app": f"WCB_{tag}_ESP32.bin", "part": f"WCB_{tag}_ESP32_part.bin", "boot": f"WCB_{tag}_ESP32_boot.bin"}


def bench_w2_image(version, folder=None):
    """The bench image W2 runs -> {"folder", "version", "tag", "names", "files"}; Skip when results/builds/
    wcb-esp32-meshq is missing a file or its app does not carry `version` (flashing it would change W2); AssertionError
    for a set that is not a usable build (w2_image_problems)."""
    folder = folder or os.path.join(builds_dir(), W2_IMAGE)
    files = {}
    for k, name in W2_FILES.items():
        p = os.path.join(folder, name)
        if not os.path.isfile(p):
            raise Skip(f"no {name} in {folder}: the bench image W2 was flashed from is not here (results/builds/"
                       f"FLASHED.md)")
        with open(p, "rb") as f:
            files[k] = f.read()
    if version.encode() not in files["app"]:
        raise Skip(f"W2 reports {version}, which the bench image in {folder} does not carry: flashing it would change "
                   f"W2's firmware")
    problems = w2_image_problems(files, version)
    if problems:
        raise AssertionError(f"{folder}: " + "; ".join(problems))
    tag, names = release_names(version)
    return {"folder": folder, "version": version, "tag": tag, "names": names, "files": files}


def seed_w2(img, boot=True, part=True):
    """A seed for run_intellex_test / a stage: the image's files under their release names in the hil-bench cache. boot /
    part False leave that file out (a build the host must refuse a full flash of)."""
    def seed(stage_dir):
        keep = {"app": True, "part": part, "boot": boot}
        seed_firmware(stage_dir, "wcb", BRANCH, {img["names"][k]: img["files"][k] for k in W2_FILES if keep[k]})
    return seed


def flash_log_problems(st, want):
    """/_api/flash-status after a flash (host.py _flash_state: running, ok, error, version, percent, log) against what the
    flash had to do -> ([problem], facts). want: ok (bool); version (the tag reported, when ok); writes (the addresses
    esptool must have written, each verified); markers (substrings some line must hold); absent (substrings no line may
    hold); error (a substring of the error, when not ok). esptool 5.3.1 prints 'Wrote <n> bytes (<c> compressed) at
    0x<addr> in ...' and then 'Hash of data verified.' for each region. Messages quote markers, addresses, counts and the
    error's first line only."""
    st = st if isinstance(st, dict) else {}
    log = [str(x).strip() for x in st.get("log") or []]
    problems = []
    if st.get("running"):
        problems.append("the flash is still running")
    ok = st.get("ok")
    err = (str(st.get("error") or "").splitlines() or [""])[0][:200]
    if ok is not want["ok"]:
        problems.append(f"flash-status ok is {ok!r}, expected {want['ok']}" + (f" (error: {err})" if err else ""))
    if want["ok"] and st.get("version") != want.get("version"):
        problems.append(f"flash-status names the build flashed {st.get('version')!r}, expected {want.get('version')!r}")
    if not want["ok"] and want.get("error") and want["error"] not in str(st.get("error") or ""):
        problems.append(f"the error does not say {want['error']!r} (it says: {err})")
    written = [int(m.group(1), 16) for x in log for m in [WROTE.search(x)] if m]
    verified = sum(1 for x in log if x == VERIFIED)
    wanted = sorted(want.get("writes") or [])
    if sorted(written) != wanted:
        problems.append(f"esptool wrote {[hex(a) for a in written]}, expected {[hex(a) for a in wanted]}")
    if verified != len(written):
        problems.append(f"{verified} '{VERIFIED}' line(s) for {len(written)} region(s) written")
    for m in want.get("markers") or ():
        if not any(m in x for x in log):
            problems.append(f"the flash log has no line with {m!r}")
    for m in want.get("absent") or ():
        hits = sum(1 for x in log if m in x)
        if hits:
            problems.append(f"the flash log has {hits} line(s) with {m!r}")
    facts = {"written": [hex(a) for a in written], "verified": verified, "lines": len(log),
             "deprecated": sum(1 for x in log if "Deprecated" in x), "percent": st.get("percent")}
    return problems, facts


def percent_path(readings):
    """The /_api/flash-status percent readings a spec took while a flash ran -> the distinct values in order (a run of
    equal readings once): [0] for a bar that never moved (INTELLEX.md finding 13)."""
    out = []
    for p in readings or ():
        if not out or out[-1] != p:
            out.append(p)
    return out


def w2_want(img, mode):
    """flash_log_problems' expectations for the Wizard's Update ('update'), Flash ('flash') and Factory Reset ('factory')
    of W2 through Intellex (wcb_flash.py flash, write_list), and for a full flash of a build with no bootloader
    ('refused')."""
    app = img["names"]["app"]
    common = ["Chip: ESP32 -> ESP32 binary", f"offline - using the cached {BRANCH} copy of {app}"]
    if mode == "refused":
        return {"ok": False, "writes": [], "error": "Cannot do a full flash: no bootloader",
                "markers": ["Chip: ESP32 -> ESP32 binary"]}
    want = {"ok": True, "version": img["tag"], "absent": ["Deprecated"], "markers": list(common)}
    if mode == "update":
        want["writes"] = [OTADATA, APP]
        want["markers"] += ["Update: app only", "OTA boot selector (0xE000) reset to ota_0; NVS/config preserved."]
    elif mode == "flash":
        want["writes"] = [BOOT, PART, OTADATA, APP]
        want["markers"] += ["OTA boot selector (0xE000) reset to ota_0; NVS/config preserved."]
    elif mode == "factory":
        want["writes"] = [BOOT, PART, NVS, OTADATA, APP]
        want["markers"] += ["Factory reset -- NVS (0x9000, 20 KB) and the OTA boot selector will be erased."]
    else:
        raise ValueError(mode)
    return want


def _flash_hook(judged, want):
    """The flash_done hook: read /_api/flash-status from the host, judge it (flash_log_problems) and keep the verdict
    for the harness's own check after the spec; the spec gets it too, and fails on it."""
    def flash_done(host, body):
        code, st = host.json("GET", "/_api/flash-status", timeout=10)
        problems, facts = flash_log_problems(st if code == 200 else {}, want)
        facts["percents"] = percent_path(body.get("percents"))
        judged.update(problems=problems, facts=facts)
        return {"problems": problems, "facts": facts}
    return flash_done


def _wait_flash(host, timeout):
    """/_api/flash-status once the flash the host is running has ended, or AssertionError after `timeout`."""
    deadline = time.monotonic() + timeout
    while True:
        code, st = host.json("GET", "/_api/flash-status", timeout=10)
        if code == 200 and isinstance(st, dict) and not st.get("running"):
            return st
        if time.monotonic() > deadline:
            raise AssertionError(f"the host's flash was still running after {timeout:.0f} s")
        time.sleep(1.0)


def _last_resort(port):
    return (f"from the repo root, `arduino-cli upload --fqbn {FQBN_W2} --input-dir tests/hil/results/builds/{W2_IMAGE} "
            f"-p {port}` (a Claude session in auto mode is refused bench uploads: Greg runs it; results/builds/"
            f"FLASHED.md)")


def park_probes(bench, wcb):
    """Release every probe channel bound to a port of WCB `wcb` before esptool resets it into its ROM loader -> how
    many. A bound channel's TX drives that WCB's RX line high (a UART idles high); a released one is a weak pull-up
    (wcb_probe unbindChannel). In full run 20260929-203948 every write-flash on W2 failed to reach download mode
    ('Wrong boot mode detected (0x17)', all 38 tries) with probe2 bound to W2's S1, S3, S4 and S5 - S3's RX is GPIO4,
    which the ESP32 latches at reset - while the same flashes passed in 20260929-203453, where no probe had bound a
    channel since its boot. The next test that wires a port binds it again (hil/links.py bind)."""
    n = 0
    for link in bench.links.all():
        if link.wcb == wcb and link.channel is not None:
            try:
                bench.links.release(link)
                n += 1
            except Exception as e:  # noqa: BLE001 - a probe that went away holds nothing
                bench.note(f"release of {link.key} before an esptool reset of W{wcb} failed: {e}")
    if n:
        bench.note(f"W{wcb}: released {n} probe channel(s) on its ports before esptool resets it")
    return n


def reflash_w2(bench, img, device="wcb2"):
    """W2 flashed again with the same image, in full (bootloader, table, app; NVS kept), through a leashed host's own
    /_api/flash-wcb with no page - the plan's first recovery: the ESP32 ROM loader answers every auto-reset -> what
    happened. AssertionError naming the last resort when W2 still does not answer."""
    port = bench.cfg["devices"][device]["port"]
    bench.close_device(device)
    park_probes(bench, bench.cfg["devices"][device]["wcb"])
    tid = "intellex.flash_w2_recovery"
    sd = stage(bench, tid, tools="none", settings=SETTINGS_W2)
    seed_w2(img)(sd)
    host = IntellexHost(bench, sd, allow_ports=[port])
    said = "the recovery flash did not start"
    try:
        host.start()
        host.attach({"kind": "serial", "port": port})
        code, body = host.json("POST", "/_api/flash-wcb", {"appOnly": False, "eraseNvs": False}, timeout=15)
        if code != 200 or not (isinstance(body, dict) and body.get("ok")):
            raise AssertionError(f"Intellex refused the recovery flash: {code} {body}")
        st = _wait_flash(host, 300)
        said = f"flashed it again in full through Intellex ({'ok' if st.get('ok') else 'FAILED'})"
    except AssertionError as e:
        said = f"the recovery flash failed ({e})"
    finally:
        host.stop()
        copy_logs(bench, sd, tid)
    try:
        _reacquire(bench, device, timeout=40)
    except AssertionError as e:
        raise AssertionError(f"{said}; W2 still does not answer ({e}). Last resort: {_last_resort(port)}") from None
    bench.note(f"{device}: {said}; it answers again")
    return f"{said}, and W2 answers again"


def _recover_w2(img):
    """run_intellex_test's recover for a W2 flash: an EN reset into the app (reset_into_app), then the same image flashed
    again through Intellex (reflash_w2)."""
    def recover(bench, device):
        try:
            return reset_into_app(bench, device)
        except AssertionError as e:
            first = str(e).splitlines()[0][:160]
        return f"an EN reset did not bring it back ({first}); " + reflash_w2(bench, img, device)
    return recover


def _slot(w):
    """The app slot a WCB runs ('app0' / 'app1', ?OTALOCAL,STATUS)."""
    return _local_status(w)["running"]


def _free(bench):
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}) and may hold a bench port - close it first")


def _w2_flash(bench, test_id, mode, push=False, extra=None, timeout=960.0):
    """Flash W2 through the Wizard inside Intellex (spec `test_id`, boardGo(<W2's slot>, {mode, pushConfig: push})) with
    the bench image it runs, and prove it came back as it was -> [problem]."""
    port = bench.cfg["devices"]["wcb2"]["port"]
    n2 = bench.cfg["devices"]["wcb2"]["wcb"]
    w2 = WCB(bench.dev("wcb2"))
    version = w2.version()
    img = bench_w2_image(version)
    slot0 = _slot(w2)
    w1 = usb_wcb(bench)
    m1 = w1.dev.mark()
    judged, problems = {}, []
    others = [n for n in bench.wcb_numbers() if n != n2]
    park_probes(bench, n2)
    with config_guard(bench, *bench.wcb_numbers()):
        try:
            run_intellex_test(bench, test_id, attach={"kind": "serial", "port": port}, device="wcb2",
                              settings=SETTINGS_W2, seed=seed_w2(img), hooks={"flash_done": _flash_hook(judged,
                                                                                                         w2_want(img, mode))},
                              args=dict({"wcb": n2, "boards": others, "mode": mode, "push": push, "tag": img["tag"],
                                         "version": version, "com": port}, **(extra or {})),
                              recover=_recover_w2(img), timeout=timeout)
        finally:
            _stop_rterm_all(bench, skip=(n2,))   # the shim routes the mesh through W2 and arms W1's terminal there
    if "problems" not in judged:
        problems.append("the spec never had the harness judge the host's flash log (hook flash_done)")
    else:
        problems += judged["problems"]
        bench.note(f"{test_id}: {judged['facts']}")
    w2 = WCB(bench.dev("wcb2"))
    now = w2.version()
    if now != version:
        problems.append(f"W2 reports {now} after the flash, {version} before")
    slot1 = _slot(w2)
    bench.note(f"{test_id}: W2 ran {slot0} before the flash and {slot1} after")
    if slot1 != "app0":
        problems.append(f"W2 runs {slot1} after the flash: Intellex writes the app to app0 (0x10000) and erases "
                        f"otadata, so it must boot app0")
    edges = _boot_edges(w1.dev, m1, n2)
    if not edges:
        problems.append(f"W1 heard no boot announce from WCB{n2} during the test: the flash never restarted W2")
    return problems


# ============================================================ W2: Update, Flash, one at a time, a refusal
@test("intellex.flash_w2_update", "The Wizard's Update FW through Intellex on W2 (COM15): offline, from a seeded "
      "hil-bench cache, the host detects the ESP32 and writes the bench image W2 runs into app0 with otadata erased - "
      "both regions' hashes verified, no esptool 4 spelling - then the Wizard reconnects and pulls W2 again; W2 ends on "
      "its version, running app0, config unchanged (opt-in intellex_flash; 1 W2 restart; IX-WP10)",
      needs=["wcb2", "wcb1"], opt_in="intellex_flash")
def flash_w2_update(bench):
    """Wizard/app.js boardGo, mode 'update' -> flashFirmware(appOnly) -> intellex_shim.js installWcbFlash ->
    POST /_api/flash-wcb {appOnly:true} -> host.py _start_flash_job (detach, wcb_flash.flash, reattach) ->
    wcb_flash.detect (esptool flash-id), fetch_images (offline: the cached listing and files), write_list (the app and
    the otadata blank), run_esptool (write-flash at 921600, keep/keep/keep, --after hard-reset). Outside the guided
    setup an Update re-pulls the board afterwards and pushes nothing (app.js, the isUpdate branch)."""
    _free(bench)
    problems = _w2_flash(bench, "intellex.flash_w2_update", "update")
    assert not problems, "; ".join(problems)


@test("intellex.flash_w2_full", "The Wizard's Flash through Intellex on W2, its config push after it left off: "
      "bootloader at 0x1000, partition table at 0x8000 and app at 0x10000 from one build, otadata erased, NVS kept, "
      "every region verified; W2 ends on its version, running app0, config unchanged (opt-in intellex_flash; "
      "1 W2 restart; IX-WP10)", needs=["wcb2", "wcb1"], opt_in="intellex_flash")
def flash_w2_full(bench):
    """mode 'flash' -> flashFirmware with appOnly and eraseNvs false -> write_list's full set (wcb_flash.py:308-324).
    The Wizard would then push W2's whole pre-flash config back (boardGo 'configure' with no baseline); pushConfig:false,
    its own switch for that, leaves the push out and only re-pulls (INTELLEX.md DX37): the flash keeps NVS, and a full
    push is the Wizard's feature, not the flash's."""
    _free(bench)
    problems = _w2_flash(bench, "intellex.flash_w2_full", "flash")
    assert not problems, "; ".join(problems)


@test("intellex.flash_one_at_a_time", "While the Wizard's Update runs on W2 through Intellex, a second flash - POST "
      "/_api/flash, the NaviCore tool's route, and /_api/flash-wcb again, from the page - gets 409 'a flash is already "
      "running' and starts nothing; the first flash completes and W2 ends as it was (opt-in intellex_flash; 1 W2 "
      "restart; IX-WP10)", needs=["wcb2", "wcb1"], opt_in="intellex_flash")
def flash_one_at_a_time(bench):
    """host.py _claim_port_for_flash: one claim covers both tools' routes, checked and taken under one lock. The stage
    caches nothing for NaviCore (branch hil-none), so even a claim that let the NaviCore route through could find no
    image to write onto W2."""
    _free(bench)
    problems = _w2_flash(bench, "intellex.flash_one_at_a_time", "update", extra={"concurrent": True})
    assert not problems, "; ".join(problems)


@test("intellex.flash_refused_board_runs", "(should) A full flash Intellex refuses after identifying W2 (the seeded build "
      "has no bootloader) leaves W2 running its app, answering on its own port with no reset from anyone - esptool's "
      "flash-id left the chip in its ROM loader ('Staying in bootloader'), and nothing is written (opt-in "
      "intellex_flash; W2 restarted 1-2 times; IX-WP10, INTELLEX.md finding 18)", needs=["wcb2", "wcb1"],
      opt_in="intellex_flash")
def flash_refused_board_runs(bench):
    """Intellex src/wcb_flash.py detect runs 'esptool --before default-reset --after no-reset flash-id' (:254-256): the
    chip is reset into its ROM loader and left there. Every refusal after it - no cached image, two app images, a full
    flash with no bootloader or table (write_list :309-323) - raises out of wcb_flash.flash (:398-415) with nothing
    written, and host.py _start_flash_job (:1021-1057) reattaches the port with DTR and RTS low
    (serial_transport.py): nothing resets the chip, so the board sits in its ROM loader until someone presses EN or
    replugs it. The Wizard's own flasher.js closes and reopens its port, whose DTR/RTS toggle resets the board (its
    comment at :736-761). Here a full flash of a build with no bootloader; the harness then opens W2's port with both
    lines low - no reset - and asks ?VERSION. A W2 left in the loader is reset into its app (reset_into_app)."""
    _free(bench)
    port = bench.cfg["devices"]["wcb2"]["port"]
    w2 = WCB(bench.dev("wcb2"))
    img = bench_w2_image(w2.version())
    tid = "intellex.flash_refused_board_runs"
    problems, stranded, recovered = [], None, None
    park_probes(bench, bench.cfg["devices"]["wcb2"]["wcb"])
    with config_guard(bench, *bench.wcb_numbers()):
        bench.close_device("wcb2")
        sd = stage(bench, tid, tools="none", settings=SETTINGS_W2)
        seed_w2(img, boot=False)(sd)
        host = IntellexHost(bench, sd, allow_ports=[port])
        st = {}
        try:
            host.start()
            host.attach({"kind": "serial", "port": port})
            code, body = host.json("POST", "/_api/flash-wcb", {"appOnly": False, "eraseNvs": False}, timeout=15)
            if code != 200 or not (isinstance(body, dict) and body.get("ok")):
                problems.append(f"the flash was not even started: {code} {body}")
            else:
                st = _wait_flash(host, 180)
        finally:
            host.stop()
            copy_logs(bench, sd, tid)
        if st:
            p, facts = flash_log_problems(st, w2_want(img, "refused"))
            problems += p
            bench.note(f"{tid}: {facts}")
        try:
            _reacquire(bench, "wcb2", timeout=15)
        except AssertionError as e:
            stranded = str(e).splitlines()[0][:160]
            try:
                recovered = reset_into_app(bench, "wcb2")
            except AssertionError as r:
                recovered = f"and the EN reset failed too ({r}); " + reflash_w2(bench, img)
    if stranded:
        bench.note(f"{tid}: W2 did not answer after the refused flash ({stranded}); {recovered}")
        problems.append(f"(should) W2 was left in its ROM loader by the refused flash - it answered nothing on its own "
                        f"port until the harness reset it ({recovered}) (INTELLEX.md finding 18)")
    assert not problems, "; ".join(problems)


# ============================================================ W2: Factory Reset (attended)
@test("intellex.flash_w2_factory", "(attended) The Wizard's Factory Reset through Intellex on W2: the bench image in "
      "full and W2's NVS erased - 0x9000 written blank, every region verified - so W2 boots on the firmware's defaults; "
      "then the harness restores W2 over its USB from its own chain and learned peers, and W2 ends on its version and "
      "config (opt-in intellex_flash_factory; 3 W2 restarts; IX-WP10)", needs=["wcb2", "wcb1"],
      opt_in="intellex_flash_factory")
def flash_w2_factory(bench):
    """mode 'factory' -> flashFirmware(eraseNvs) -> write_list with the NVS blank (wcb_flash.py:326-330). pushConfig:false
    leaves out the Wizard's full push of the old config (DX37); W2 is put back the way nvs.erase_defaults_restore puts W1
    back (suites/s31_password_erase.py _erase_cycle): ?HW and a boot, WDP off and the Maestro table cleared, every line
    of W2's own chain, WDP back, a boot, its learned peers. Its chain carries the mesh password and WiFi credentials: it
    is read from W2 and given back to W2 only, never noted."""
    from suites.s27_identity_destructive import _learned_peers
    from suites.s31_password_erase import _replay, _replayable
    _free(bench)
    n2 = bench.cfg["devices"]["wcb2"]["wcb"]
    w2 = WCB(bench.dev("wcb2"))
    tokens = w2.backup_chain()[0]
    _replayable(tokens)
    hw, q = token(tokens, "?HW,"), token(tokens, "?WCBQ,")
    if hw is None or hw.upper() == "?HW,0" or q is None:
        raise Skip("W2's chain has no hardware version or ?WCBQ to restore")
    floor = int(q.split(",")[1])
    learned = _learned_peers(w2, floor)
    bench.note(f"intellex.flash_w2_factory: W2's learned peers before: {learned}")
    problems, judged = [], {}
    with config_guard(bench, *bench.wcb_numbers()):
        try:
            problems += _w2_flash_factory(bench, n2, judged)
        finally:
            w2 = WCB(bench.dev("wcb2"))
            after = w2.backup_chain()[0]
            if after == tokens:
                if judged:
                    problems.append("W2's chain is unchanged after the Factory Reset: its NVS was not erased")
            else:
                if token(after, "?WCB,") != "?WCB,1" or token(after, "?HW,") not in (None, "?HW,0"):
                    problems.append("after the Factory Reset W2's chain is not the firmware's defaults (WCB 1, no "
                                    "hardware version)")
                w2.run(hw)                       # the pin map first: until a boot on it no serial port is usable
                w2.reboot()
                w2.run("?WDP,OFF")
                w2.run("?MAESTRO,CLEAR,ALL", timeout=8)
                refused = _replay(w2, tokens)
                if "?WDP,OFF" not in tokens:
                    w2.run("?WDP,ON")
                if refused:
                    problems.append("lines refused on the restore: " + "; ".join(refused))
                w2.reboot()
                for n in learned:
                    w2.run(f"?WDP,ADD,{n}")
                time.sleep(6)                    # LEARNED_FLUSH_DEBOUNCE_MS, then the NVS write
    now = _learned_peers(WCB(bench.dev("wcb2")), floor)
    if now != learned:
        problems.append(f"W2's learned peers after the restore: {now}, before: {learned}")
    assert not problems, "; ".join(problems)


def _w2_flash_factory(bench, n2, judged):
    """The Factory Reset itself (_w2_flash's checks but the config: the harness restores it afterwards) -> [problem];
    `judged` gets the flash log's verdict (the hook), which is also how the caller knows the flash ran."""
    port = bench.cfg["devices"]["wcb2"]["port"]
    w2 = WCB(bench.dev("wcb2"))
    version = w2.version()
    img = bench_w2_image(version)
    w1 = usb_wcb(bench)
    m1 = w1.dev.mark()
    problems = []
    park_probes(bench, n2)
    try:
        run_intellex_test(bench, "intellex.flash_w2_factory", attach={"kind": "serial", "port": port}, device="wcb2",
                          settings=SETTINGS_W2, seed=seed_w2(img),
                          hooks={"flash_done": _flash_hook(judged, w2_want(img, "factory"))},
                          args={"wcb": n2, "boards": [n for n in bench.wcb_numbers() if n != n2], "mode": "factory",
                                "push": False, "tag": img["tag"], "version": version, "com": port},
                          recover=_recover_w2(img), timeout=960)
    finally:
        _stop_rterm_all(bench, skip=(n2,))
    if "problems" not in judged:
        problems.append("the spec never had the harness judge the host's flash log (hook flash_done)")
    else:
        problems += judged["problems"]
        bench.note(f"intellex.flash_w2_factory: {judged['facts']}")
    now = WCB(bench.dev("wcb2")).version()
    if now != version:
        problems.append(f"W2 reports {now} after the flash, {version} before")
    if not _boot_edges(w1.dev, m1, n2):
        bench.note("intellex.flash_w2_factory: W1 heard no boot announce from W2 (after the erase W2 runs as WCB 1 on "
                   "the default mesh password, which W1 does not accept)")
    return problems


# ============================================================ NaviCore: its app through the config tool
def _recover_nc(folder):
    """run_intellex_test's recover for NaviCore: ncflash's ladder (PING, the USB-Serial/JTAG reset, and with
    navicore_esptool ticked its esptool rungs writing the bench image into app0)."""
    def recover(bench, device):
        allow = "navicore_esptool" in optin.enabled(bench.cfg)
        r = ncflash.recover(NaviCore(bench.dev(device)), known_good=folder, allow_esptool=allow,
                            what="intellex.flash_navicore_app recovery")
        return f"ncflash.recover brought it back by its {r['rung']} rung ({r['version']})"
    return recover


def _record_nc_flash(bench, info, folder, tid, st0, st1, judged, t0):
    """A FLASHED.md row (ncflash.record_flash) for a flash that reached NaviCore - the spec had the log judged, or
    NaviCore restarted during the test - and none for a test that stopped before flashing. 'OK' only when STATUS shows
    the image's App SHA256 running from app0: last_written() takes an OK row as what the board runs."""
    try:
        restarted = _restarted(_uptime_ms(NaviCore(bench.dev("navicore"))), (time.monotonic() - t0) * 1000)
    except AssertionError:
        restarted = True                         # no answer: the flash may have left it anywhere
    if not judged and not restarted:
        return None
    sha = info["elf_sha"][:16]
    good = bool(st1) and st1["app_sha"] == sha and bool(st1["running"]) and st1["running"]["addr"] == APP
    after = ncflash._slot_name(st1["running"]) if st1 else "STATUS did not answer"
    result = (f"{'OK' if good else 'NOT VERIFIED'}: {ncflash._slot_name(st0['running'])} -> {after}, App SHA256 "
              f"{(st1 or {}).get('app_sha')}, {time.monotonic() - t0:.0f} s")
    manifest = info.get("manifest")
    return ncflash.record_flash(builds_dir(), folder=folder, elf_sha=info["elf_sha"],
                                tree=ncflash.tree_line((manifest or {}).get("navicore")),
                                how="Intellex /_api/flash (esptool: app0 + otadata erased)", result=result,
                                what="; ".join(x for x in (tid, ncflash.build_line(manifest)) if x))


def nc_want(version, name):
    """flash_log_problems' expectations for the config tool's Update Firmware through Intellex with only the app cached
    (flash.py fetch_images: no bootloader and table on the branch -> app only; write_list: otadata always)."""
    return {"ok": True, "version": version, "writes": [OTADATA, APP],
            "markers": ["Note: bootloader/partition files not on GitHub -- flashing app only.",
                        f"offline - using the cached {BRANCH} copy of {name}",
                        "OTA boot selector (0xE000) reset to ota_0 (config preserved)."]}


@test("intellex.flash_navicore_app", "The config tool's Update Firmware through Intellex on NaviCore's COM port writes "
      "the bench image NaviCore runs into app0 alone - offline from a seeded cache, otadata erased, both regions "
      "verified - and NaviCore comes back on app0 with the same App SHA256 and version, the tool reconnected, its "
      "config, library, clips and peers unchanged (opt-in intellex_flash_navicore; 1 NaviCore restart; IX-WP10)",
      needs=["navicore"], opt_in="intellex_flash_navicore")
def flash_navicore_app(bench):
    """intellex_shim.js wireNativeFlash (the Update Firmware button's click taken in the capture phase) ->
    startNativeFlash -> POST /_api/flash {eraseNvs:false} -> host.py _start_flash_job -> flash.py flash: fetch_images
    (offline: the cached listing and app; no bootloader and table on the branch, so app only), write_list (otadata blank),
    run_esptool (esptool 4 spellings: finding 14's 'Deprecated:' lines are counted, not failed here). esptool resets the
    S3 through its USB-Serial/JTAG and hard-resets it after; NaviCore boots app0. Identity: the App SHA256
    ?OTALOCAL,STATUS prints is the image's ELF SHA-256 before and after (ncflash), and a FLASHED.md row records the
    flash. Inside nc_guard (config, library, clips, peers, mode), skipped while the recorder holds anything (D33)."""
    _free(bench)
    folder = os.path.join(builds_dir(), ncflash.BENCH_IMAGE)
    try:
        info = ncflash.check_image(folder)
    except ncflash.ImageError as e:
        raise Skip(f"the bench NaviCore image is not usable here: {str(e)[:200]}")
    sha = info["elf_sha"][:16]
    port = bench.cfg["devices"]["navicore"]["port"]
    tid = "intellex.flash_navicore_app"
    problems, judged = [], {}
    with nc_guard(bench) as g:
        _restartable(g.nc)
        st0 = ncflash.ota_status(g.nc)
        if st0["app_sha"] != sha:
            raise Skip(f"NaviCore runs App SHA256 {st0['app_sha']}, not the bench image {ncflash.BENCH_IMAGE} ({sha}): "
                       f"flashing it would change NaviCore")
        version = g.nc.ping()
        if version != info["version"]:
            raise Skip(f"NaviCore answers {version}; the bench image carries {info['version']}")
        name = f"NaviCore_{version}_ESP32S3.bin"
        w1 = usb_wcb(bench) if bench.has("wcb1") else None
        m1 = w1.dev.mark() if w1 else None
        t0 = time.monotonic()
        st1 = None
        try:
            run_intellex_test(bench, tid, attach={"kind": "serial", "port": port}, device="navicore",
                              settings=SETTINGS_NC,
                              seed=lambda sd: seed_firmware(sd, "navicore", BRANCH, {name: info["data"]}),
                              hooks={"flash_done": _flash_hook(judged, nc_want(version, name))},
                              args={"version": version, "com": port}, recover=_recover_nc(folder), timeout=660)
        finally:
            g.nc = NaviCore(bench.dev("navicore"))      # nc_guard restores through the port as it is now
            _nc_quiet(bench)
            try:
                st1 = ncflash.ota_status(g.nc)
            except AssertionError as e:
                problems.append(f"?OTALOCAL,STATUS after the flash: {str(e)[:160]}")
            try:
                _record_nc_flash(bench, info, folder, tid, st0, st1, judged, t0)
            except Exception as e:  # noqa: BLE001 - a FLASHED.md write must not hide the test's own result
                bench.note(f"{tid}: the FLASHED.md row was not written: {type(e).__name__}: {e}")
    if "problems" not in judged:
        problems.append("the spec never had the harness judge the host's flash log (hook flash_done)")
    else:
        problems += judged["problems"]
        bench.note(f"{tid}: {judged['facts']}")
    if st1:
        if st1["app_sha"] != sha:
            problems.append(f"NaviCore runs App SHA256 {st1['app_sha']} after the flash, the bench image is {sha}")
        if not st1["running"] or st1["running"]["addr"] != APP:
            problems.append(f"NaviCore runs {ncflash._slot_name(st1['running'])} after the flash: Intellex writes app0 "
                            f"(0x10000) and erases otadata")
    nc = NaviCore(bench.dev("navicore"))
    now = nc.ping()
    if now != version:
        problems.append(f"NaviCore answers {now} after the flash, {version} before")
    if not _restarted(_uptime_ms(nc), (time.monotonic() - t0) * 1000):
        problems.append("NaviCore's uptime shows no restart: the flash never reset it")
    if w1 and not _boot_edges(w1.dev, m1, NAVICORE_ID):
        problems.append(f"W1 heard no boot announce from WCB{NAVICORE_ID} during the test")
    assert not problems, "; ".join(problems)
