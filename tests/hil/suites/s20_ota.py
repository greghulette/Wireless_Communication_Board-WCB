"""OTA — ?OTALOCAL over USB and ?OTA relayed over the mesh.

Built from the verified ota specs. Rules from the specs:
- Every BEGIN that passes the guards erases at least 4 KB of the INACTIVE app slot (and a full-size BEGIN the whole
  image area). The running app and otadata are untouched, but nothing can restore that slot and ?backup cannot see
  it, so every such test is opt-in (bench.json "opt_in"): "ota_erase" for small sessions, "ota_full" for full-image
  transfers on W1, "ota_full_wcb2" for the relayed full transfer to W2, "ota_wrong_chip" for the S3-image test.
  Rejected BEGINs (guards before esp_ota_begin, WCB_OTA.cpp:108-124) and sessions-less commands run normally.
- END OK switches otadata; full transfers always run twice so the board ends on its original slot.
- One OTA session per board, shared by USB and relay; any BEGIN supersedes and any ABORT (any id) kills it.
- ?OTALOCAL,BAUD switches the USB rate: sent with send(), never run() (the echo would cross the switch), and the host
  follows only after the ACK plus a margin. _recover_local leaves W1 idle at 115200 whatever happened.
- Never send ?OTA,BEGIN with family 1, or any DATA/END, to NaviCore (20).
"""
import base64
import glob
import os
import re
import time
import zlib
from contextlib import contextmanager

from hil.runner import Skip, test
from suites.common import Console, Watch, config_guard, link, nonce, usb_wcb

SLOTS = {"app0": "010000", "app1": "1f0000"}          # min_spiffs.csv:4-5
SLOT_SIZE = 1966080
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _has(lines, text):
    return any(text in x for x in lines)


def _ota(lines):
    return [x.rstrip() for x in lines if x.startswith("[OTA")]


def _b64(data):
    return base64.b64encode(data).decode()


def _crc(text):
    return "%08X" % (zlib.crc32(text.encode()) & 0xFFFFFFFF)


def _session_id():
    return int(nonce(), 16) % 60000 + 1


def _image(w, chip="ESP32"):
    """The image the board is running (Skip if none is found), sanity-checked.

    First the bench build the harness flashes from (results/builds/wcb-esp32-meshq, see its FLASHED.md), then the CI
    release in Code/bin. A local build keeps the committed version string, so the bench boards report a version with
    no release file of that name. And even a release with a matching name would not be the image they run, so a
    full 'same image' OTA from Code/bin would change their firmware. The firmware reports nothing more specific than
    its version string, so this relies on the bench convention that W1/W2 are flashed from the builds folder."""
    version = w.version()
    bench_build = os.path.join(REPO, "tests", "hil", "results", "builds", "wcb-esp32-meshq", "WCB.ino.bin")
    paths = [bench_build] if chip == "ESP32" and os.path.exists(bench_build) \
        and version.encode() in open(bench_build, "rb").read() else []
    paths += [p for p in glob.glob(os.path.join(REPO, "Code", "bin", f"WCB_{version}_*.bin")) if p.endswith(f"_{chip}.bin")]
    if not paths:
        raise Skip(f"no image of the running firmware {version}: neither the bench build "
                   f"(results/builds/wcb-esp32-meshq) nor Code/bin/WCB_{version}_*_{chip}.bin carries it")
    data = open(paths[0], "rb").read()
    if data[:1] != b"\xe9" or version.encode() not in data or len(data) > SLOT_SIZE:
        raise AssertionError(f"{os.path.basename(paths[0])} does not look like a {chip} image of {version}")
    return data


def _status(lines):
    """The 7-line ?OTALOCAL,STATUS block -> dict(family, firmware, running, next, session)."""
    lines = [x.rstrip() for x in lines]
    if any(x.startswith("Next (OTA):  none") for x in lines):
        raise Skip("the partition table has no spare OTA slot")

    def get(rx):
        return next((m for x in lines for m in [re.match(rx, x)] if m), None)

    chip, fw = get(r"^Chip:        .+ \(family (\d)\)$"), get(r"^Firmware:    (\S+)$")
    run = get(rf"^Running:     '(app[01])' @0x([0-9a-f]{{6}}) \({SLOT_SIZE} B\)$")
    nxt = get(rf"^Next \(OTA\):  '(app[01])' @0x([0-9a-f]{{6}}) \({SLOT_SIZE} B\)$")
    session = get(r"^Session:     (.+)$")
    if "---------- OTA Status ----------" not in lines or not all((chip, fw, run, nxt, session)):
        raise AssertionError(f"malformed OTA STATUS block: {lines}")
    for m in (run, nxt):
        if SLOTS[m.group(1)] != m.group(2):
            raise AssertionError(f"slot {m.group(1)} reported at 0x{m.group(2)}")
    return dict(family=int(chip.group(1)), firmware=fw.group(1), running=run.group(1), next=nxt.group(1), session=session.group(1))


def _local_status(w):
    return _status(w.run("?OTALOCAL,STATUS"))


def _recover_local(w):
    """Leave W1 idle at 115200. ?OTALOCAL,ABORT prints nothing when idle but always restores the board's rate
    (WCB_OTA.cpp:94-95), so it goes at the rate host and board last agreed on, then the host follows. If the board
    still does not answer, it is stranded at the raised rate (the rejected-re-BEGIN finding): ABORT there too."""
    w.dev.send("?OTALOCAL,ABORT")
    time.sleep(0.2)
    w.dev.set_baud(115200)
    time.sleep(0.2)
    try:
        w.version()
        return
    except AssertionError:
        pass
    w.dev.set_baud(921600)
    w.dev.send("?OTALOCAL,ABORT")
    time.sleep(0.2)
    w.dev.set_baud(115200)
    time.sleep(0.2)
    w.version()


@contextmanager
def _local(w):
    try:
        yield
    finally:
        _recover_local(w)


def _relay(w, cmd, target, session, timeout=4.0):
    """Send one ?OTA relay line; return (offset, status) from its [OTA:ACK,<target>,<session>,...] line."""
    m = w.dev.mark()
    w.send(cmd)
    g = w.dev.expect(rf"^\[OTA:ACK,{target},{session},(\d+),(\d+)\]$", timeout=timeout, since=m)
    return int(g.group(1)), int(g.group(2))


def _crun(console, cmd, wait=1.0):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


# ============================================================ read-only and rejected-before-erase
@test("ota.status_local", "?OTALOCAL,STATUS on W1: chip family 0, running and next min_spiffs slots, idle session", needs=["wcb1"], links=[])
def status_local(bench):
    w = usb_wcb(bench)
    st, fw = _local_status(w), w.version()
    assert st["family"] == 0, f"family {st['family']} on a classic ESP32"
    assert st["firmware"] == fw, f"STATUS firmware {st['firmware']} vs ?VERSION {fw}"
    assert st["running"] != st["next"], "running and next are the same slot"
    assert st["session"] == "idle", f"session {st['session']}"


@test("ota.local_parse_help_unknown", "?OTALOCAL parsing: case-insensitive, bare and trailing-comma forms mean STATUS, the unknown-sub message, ?OTALOCAL? / ?OTA? print help only", needs=["wcb1"], links=[])
def local_parse_help_unknown(bench):
    w = usb_wcb(bench)
    for cmd in ("?otalocal", "?OtaLocal,status", "?OTALOCAL,"):
        assert _status(w.run(cmd))["session"] == "idle", f"{cmd} did not print an idle STATUS"
    foo = _ota(w.run("?OTALOCAL,foo"))
    assert foo == ["[OTA] unknown subcommand 'FOO' (use STATUS|BAUD|BEGIN|DATA|END|ABORT)"], foo   # BAUD listed since tracker #30
    helped = [x.rstrip() for x in w.run("?OTALOCAL?")]
    for want in ("Firmware Update (OTA)", "  STATUS                    Show OTA slot / session state"):
        assert _has(helped, want), f"?OTALOCAL? lacks {want!r}"
    assert "---------- OTA Status ----------" not in helped, "?OTALOCAL? ran STATUS as well as printing help"
    relay_help = w.run("?OTA?")
    assert _has(relay_help, "Firmware Update (OTA)") and not _has(relay_help, "[OTA] relay:"), "?OTA? parsed or sent something"
    assert _local_status(w)["session"] == "idle"


@test("ota.help_matches_parser", "(should) ?OTA help shows the <session> field the relay parser requires, and the unknown-subcommand hint lists BAUD", needs=["wcb1"], links=[])
def help_matches_parser(bench):
    """Doc bugs in firmware text: the ?OTA help (WCB_Help.cpp:713-715) gives BEGIN,<target>,<imageSize>,<fam> without the
    <session> the parser needs (WCB_OTA.cpp:460-476), so following it shifts every argument ('BEGIN,2,4096,0' is session
    4096, size 0); and the unknown-subcommand hint (WCB_OTA.cpp:324) omits the valid BAUD (WCB_OTA.cpp:237)."""
    w = usb_wcb(bench)
    helped = [x for x in w.run("?OTA?") if "BEGIN,<target>" in x or "DATA,<target>" in x or "END,<target>" in x]
    hint = _ota(w.run("?OTALOCAL,foo"))
    assert helped and all("<session>" in x for x in helped), f"relay help lines without <session>: {helped}"
    assert hint and "BAUD" in hint[0], f"unknown-subcommand hint: {hint}"


@test("ota.status_remote_wcb2", "W2's OTA slot state: chip family 0, slots, idle session", needs=["wcb1"], links=[])
def status_remote_wcb2(bench):
    with Console(bench, 2) as c2:
        st = _status(_crun(c2, "?OTALOCAL,STATUS"))
    assert st["family"] == 0 and st["session"] == "idle" and st["running"] != st["next"], st
    bench.note(f"W2 runs from {st['running']}, firmware {st['firmware']}")


@test("ota.local_nosession_and_usage_errors", "BAUD/DATA/END/ABORT with no session and BEGIN/DATA usage errors: exact lines, no machine marker on usage errors, no state change", needs=["wcb1"], links=[])
def local_nosession_and_usage_errors(bench):
    w = usb_wcb(bench)
    if _local_status(w)["session"] != "idle":
        raise Skip("W1 has an OTA session open")
    begin_usage = ["[OTA] BEGIN usage: ?OTALOCAL,BEGIN,<imageSize>,<family 0|1>"]
    checks = [("?OTALOCAL,BAUD,921600", ["[OTA:BAUD,ERR,115200]"]),
              ("?OTALOCAL,DATA,0,AAAA", ["[OTA] DATA rejected at offset 0 (write cursor at 0)", "[OTA:NAK,0]"]),
              ("?OTALOCAL,END", ["[OTA] END: no matching active session", "[OTA:END,ERR]"]),
              ("?OTALOCAL,ABORT", []),
              ("?OTALOCAL,BEGIN,4096", begin_usage), ("?OTALOCAL,BEGIN", begin_usage),
              ("?OTALOCAL,DATA,0", ["[OTA] DATA usage: ?OTALOCAL,DATA,<offset>,<base64>"])]
    with _local(w):
        bad = [f"{cmd}: {got}" for cmd, want in checks for got in [_ota(w.run(cmd))] if got != want]
        idle = _local_status(w)["session"] == "idle"
    assert not bad, "; ".join(bad)
    assert idle, "a session-less command opened a session"


@test("ota.local_begin_rejects", "BEGIN guard rejections (chip family 1, size 0, size over the slot) print their reason and leave the session idle, before any erase", needs=["wcb1"], links=[])
def local_begin_rejects(bench):
    """Never send a non-numeric or >255 family: toInt()/uint8_t turns it into 0, which is accepted and erases
    (WCB_OTA.cpp:270). The guards precede esp_ota_begin (WCB_OTA.cpp:108-124 vs 126); timing cannot prove it."""
    w = usb_wcb(bench)
    before = _local_status(w)
    nxt = before["next"]
    checks = [("?OTALOCAL,BEGIN,4096,1", ["[OTA] BEGIN rejected: image chip family 1 != this board 0 (brick guard)", "[OTA:BEGIN,ERR,0]"]),
              ("?OTALOCAL,BEGIN,0,0", [f"[OTA] BEGIN rejected: image 0 B exceeds partition '{nxt}' ({SLOT_SIZE} B)", "[OTA:BEGIN,ERR,0]"]),
              (f"?OTALOCAL,BEGIN,{SLOT_SIZE + 1},0", [f"[OTA] BEGIN rejected: image {SLOT_SIZE + 1} B exceeds partition '{nxt}' ({SLOT_SIZE} B)", "[OTA:BEGIN,ERR,0]"])]
    with _local(w):
        bad = [f"{cmd}: {got}" for cmd, want in checks for got in [_ota(w.run(cmd, timeout=10))] if got != want]
        after = _local_status(w)
    assert not bad, "; ".join(bad)
    assert after["session"] == "idle" and after["running"] == before["running"], after


@test("ota.relay_local_rejects", "?OTA relay rejects on W1 that send nothing: bad target, subcommand and usage, CRC mismatch, base64 errors", needs=["wcb1"], links=[])
def relay_local_rejects(bench):
    w = usb_wcb(bench)
    big = _b64(bytes(range(193)))              # 193 B > the relay packet's 192-B payload
    dropped = "[OTA] relay DATA @0 DROPPED: crc 0BC0B3E6 != DEADBEEF (b64 4 chars)"
    checks = [("?OTA,BEGIN", "[OTA] relay: invalid target 0"), ("?OTA,DATA,21,1,0,AAAA", "[OTA] relay: invalid target 21"),
              ("?OTA,foo,2,1", "[OTA] relay: unknown subcommand 'FOO'"),
              ("?OTA,DATA,2,7", "[OTA] relay DATA: ?OTA,DATA,<t>,<s>,<offset>[:<crc32>],<b64>"),
              ("?OTA,DATA,2,4242,0:DEADBEEF,AAAA", dropped), ("?OTA,DATA,2,4242,0:deadbeef,AAAA", dropped),
              (f"?OTA,DATA,2,4242,0:{_crc('0,' + big)},{big}", "[OTA] relay DATA base64 error -42"),
              (f"?OTA,DATA,2,4242,0:{_crc('0,@@@@')},@@@@", "[OTA] relay DATA base64 error -44")]
    m = w.dev.mark()
    bad = [f"{cmd[:30]}: lacks {want!r}" for cmd, want in checks if not _has(w.run(cmd), want)]
    time.sleep(3)
    lines = w.dev.since(m)
    assert not bad, "; ".join(bad)
    assert not _has(lines, "[OTA:ACK,"), "a rejected relay line still reached a target"
    assert not _has(lines, "relay DATA has no crc32 suffix"), "the once-per-boot no-suffix warning was spent"


@test("ota.relay_crc_ok_and_nocrc_no_session", "Relay DATA reaches W2 with or without the CRC suffix; with no session it ACKs status ERR at cursor 0; the no-suffix warning prints at most once per boot", needs=["wcb1"], links=[])
def relay_crc_ok_and_nocrc_no_session(bench):
    """Doc gap: docs/WCB_OTA_TECHNICAL.md:224 says the relay's ACK print stays in the receive callback; the code defers
    it through the relay queue (WCB_OTA.cpp:450-457, WCB.ino:409-413). The Wizard never sends the CRC suffix
    (app.js:1914), so every Wizard relay OTA is unprotected; NaviCore's config tool sends it."""
    w = usb_wcb(bench)
    with Console(bench, 2) as c2:
        if _status(_crun(c2, "?OTALOCAL,STATUS"))["session"] != "idle":
            raise Skip("W2 has an OTA session open")
    m = w.dev.mark()
    first = _relay(w, "?OTA,DATA,2,4242,0:0BC0B3E6,AAAA", 2, 4242)
    clean = not _has(w.dev.since(m), "DROPPED")
    m2 = w.dev.mark()
    second = _relay(w, "?OTA,DATA,2,4343,0,AAAA", 2, 4343)
    warned = _has(w.dev.since(m2), "[OTA] relay DATA has no crc32 suffix")
    m3 = w.dev.mark()
    third = _relay(w, "?OTA,DATA,2,4343,0,AAAA", 2, 4343)
    bench.note(f"no-suffix warning on the first suffix-less send: {warned} (absent if already printed since W1's boot)")
    assert first == (0, 1) and clean, f"CRC-valid frame: ACK {first}, dropped {not clean}"
    assert second == (0, 1) and third == (0, 1), f"suffix-less frames: {second}, {third}"
    assert not _has(w.dev.since(m3), "no crc32 suffix"), "the no-suffix warning printed twice"


@test("ota.relay_begin_rejects", "Relay BEGIN rejected on W2 (chip family, oversize): ACK status 1, session idle, nothing erased", needs=["wcb1"], links=[])
def relay_begin_rejects(bench):
    """Never omit the family: '?OTA,BEGIN,2,<s>,4096' defaults it to 0 (WCB_OTA.cpp:485), which is accepted and erases."""
    w = usb_wcb(bench)
    s1, s2 = _session_id(), _session_id()
    with Console(bench, 2) as c2:
        nxt2 = _status(_crun(c2, "?OTALOCAL,STATUS"))["next"]
        cm = c2.mark()
        ack1 = _relay(w, f"?OTA,BEGIN,2,{s1},4096,1", 2, s1, timeout=8)
        ack2 = _relay(w, f"?OTA,BEGIN,2,{s2},3000000,0", 2, s2, timeout=8)
        time.sleep(1.0)
        lines = c2.lines(cm)
        st = _status(_crun(c2, "?OTALOCAL,STATUS"))
    assert ack1 == (0, 1) and ack2 == (0, 1), f"ACKs {ack1}, {ack2}"
    assert _has(lines, "[OTA] BEGIN rejected: image chip family 1 != this board 0 (brick guard)"), "no family reject line on W2"
    assert _has(lines, f"[OTA] BEGIN rejected: image 3000000 B exceeds partition '{nxt2}' ({SLOT_SIZE} B)"), "no size reject line on W2"
    assert st["session"] == "idle", st


@test("ota.relay_navicore_brick_guard", "A relay BEGIN with family 0 to NaviCore (20, ESP32-S3) is rejected by its brick guard before any erase", needs=["wcb1", "navicore"], links=[])
def relay_navicore_brick_guard(bench):
    """NEVER family 1, DATA or END to 20: family 1 passes NaviCore's guard and erases its inactive slot
    (navicore_ota.h:154). The family check precedes the erase (navicore_ota.h:143-147)."""
    w = usb_wcb(bench)
    nav = bench.dev("navicore")
    s = _session_id()
    nm = nav.mark()
    ack = _relay(w, f"?OTA,BEGIN,20,{s},4096,0", 20, s, timeout=8)
    nav.expect(r"\[OTA\] BEGIN rejected: image chip family 0 != this board 1 \(brick guard\)", timeout=5, since=nm)
    assert ack == (0, 1), f"NaviCore ACKed {ack}"


# ============================================================ OPT-IN ota_erase: small local sessions on W1
ABORTED = "[OTA] aborted: local abort command (current app intact)"


def _line_time(dev, since, pattern):
    rx = re.compile(pattern)
    return next((ts for ts, x in list(dev.lines[since:]) if rx.search(x)), None)


def _begin(w, size, timeout=10):
    lines = _ota(w.run(f"?OTALOCAL,BEGIN,{size},0", timeout=timeout))
    if "[OTA:BEGIN,OK,0]" not in lines:
        raise AssertionError(f"BEGIN,{size},0 printed {lines}")
    return lines


def _data(w, offset, chunk):
    return _ota(w.run(f"?OTALOCAL,DATA,{offset},{_b64(chunk)}"))


@test("ota.local_begin_abort", "OPT-IN (ota_erase): a small BEGIN opens session 1 on the inactive slot; ABORT closes it with no boot switch", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_begin_abort(bench):
    w = usb_wcb(bench)
    with _local(w):
        before = _local_status(w)
        begin = _begin(w, 4096)
        active = _local_status(w)
        abort = _ota(w.run("?OTALOCAL,ABORT"))
        after = _local_status(w)
    nxt = before["next"]
    assert begin == [f"[OTA] BEGIN ok: session 1, 4096 B → partition '{nxt}' @0x{SLOTS[nxt]} ({SLOT_SIZE} B)", "[OTA:BEGIN,OK,0]"], begin
    assert active["session"] == "ACTIVE id=1  0 / 4096 B", active["session"]
    assert abort == [ABORTED], abort
    assert after["session"] == "idle" and after["running"] == before["running"], after


@test("ota.local_begin_supersede", "OPT-IN (ota_erase): a second BEGIN silently replaces a live session: no abort line, cursor back to 0, old offsets NAK", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_begin_supersede(bench):
    w = usb_wcb(bench)
    img = _image(w)
    with _local(w):
        _begin(w, 4096)
        first = _data(w, 0, img[0:1024])
        again = _begin(w, 4096)
        active = _local_status(w)
        gap = _data(w, 1024, img[1024:2048])
        rewrite = _data(w, 0, img[0:1024])
        abort = _ota(w.run("?OTALOCAL,ABORT"))
    assert first == ["[OTA:ACK,1024]"], first
    assert not _has(again, "aborted"), f"the superseding BEGIN printed an abort: {again}"
    assert active["session"] == "ACTIVE id=1  0 / 4096 B", active["session"]
    assert gap == ["[OTA] DATA rejected at offset 1024 (write cursor at 0)", "[OTA:NAK,0]"], gap
    assert rewrite == ["[OTA:ACK,1024]"], rewrite
    assert abort == [ABORTED], abort


@test("ota.local_cursor_nak_incomplete", "OPT-IN (ota_erase): the in-order cursor ACKs, NAKs a duplicate and a gap, shows in STATUS, and END refuses an incomplete image", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_cursor_nak_incomplete(bench):
    w = usb_wcb(bench)
    img = _image(w)
    a, b = img[0:1024], img[1024:2048]
    with _local(w):
        _begin(w, 2048)
        d1, d2, d3 = _data(w, 0, a), _data(w, 0, a), _data(w, 2048, b)
        active = _local_status(w)
        m = w.dev.mark()
        end = _ota(w.run("?OTALOCAL,END"))
        time.sleep(5)
        rebooted = _has(w.dev.since(m), "Reset reason:")
        after = _local_status(w)
    assert d1 == ["[OTA:ACK,1024]"], d1
    assert d2 == ["[OTA] DATA rejected at offset 0 (write cursor at 1024)", "[OTA:NAK,1024]"], d2
    assert d3 == ["[OTA] DATA rejected at offset 2048 (write cursor at 1024)", "[OTA:NAK,1024]"], d3
    assert active["session"] == "ACTIVE id=1  1024 / 2048 B", active["session"]
    assert end == ["[OTA] END rejected: incomplete 1024 / 2048 B", "[OTA:END,ERR]"], end
    assert not rebooted and after["session"] == "idle", after


@test("ota.local_truncated_verify_fail", "OPT-IN (ota_erase): a complete but truncated image (the real image's first 2 KB) fails verify at END; no reboot, no slot change", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_truncated_verify_fail(bench):
    """If END ever printed [OTA:END,OK] the board would reboot into a 2 KB non-image; recovery is a USB reflash of W1."""
    w = usb_wcb(bench)
    img = _image(w)
    with _local(w):
        before = _local_status(w)
        _begin(w, 2048)
        d1, d2 = _data(w, 0, img[0:1024]), _data(w, 1024, img[1024:2048])
        m = w.dev.mark()
        w.send("?OTALOCAL,END")
        w.dev.expect(r"^\[OTA:END,", timeout=5, since=m)
        time.sleep(6)
        lines, raw = _ota(w.dev.since(m)), w.dev.since(m)
        after = _local_status(w)
    assert d1 == ["[OTA:ACK,1024]"] and d2 == ["[OTA] 2048 / 2048 B (100%)", "[OTA:ACK,2048]"], (d1, d2)
    assert any(re.match(r"^\[OTA\] END verify FAILED: (\S+) \(image rejected, current app intact\)$", x) for x in lines), lines
    assert "[OTA:END,ERR]" in lines and "[OTA:END,OK]" not in lines, lines
    assert not _has(raw, "Reset reason:") and not _has(raw, "Raw Serial Forwarding Task Created"), "W1 rebooted"
    assert after["running"] == before["running"] and after["session"] == "idle", after


@test("ota.local_bad_magic", "OPT-IN (ota_erase): a first chunk without the 0xE9 image magic is refused and ends the session; resending from 0 NAKs forever", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_bad_magic(bench):
    w = usb_wcb(bench)
    img = _image(w)
    with _local(w):
        _begin(w, 1024)
        zeros = _data(w, 0, bytes(1024))                      # an IDF 'E (...)' log may also appear; it is not [OTA
        after = _local_status(w)
        retry = _data(w, 0, img[0:1024])
    assert zeros == ["[OTA] esp_ota_write failed @0: ESP_ERR_OTA_VALIDATE_FAILED — aborting",
                     "[OTA] DATA rejected at offset 0 (write cursor at 0)", "[OTA:NAK,0]"], zeros
    assert after["session"] == "idle", after
    assert retry == ["[OTA] DATA rejected at offset 0 (write cursor at 0)", "[OTA:NAK,0]"], retry


@test("ota.local_overrun", "OPT-IN (ota_erase): a chunk that overruns the declared image size aborts the session", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_overrun(bench):
    w = usb_wcb(bench)
    img = _image(w)
    with _local(w):
        _begin(w, 1000)
        over = _data(w, 0, img[0:1024])
        after = _local_status(w)
    assert over == ["[OTA] write overruns image (0 + 1024 > 1000) — aborting", "[OTA] DATA rejected at offset 0 (write cursor at 0)", "[OTA:NAK,0]"], over
    assert after["session"] == "idle", after


@test("ota.local_base64_errors", "OPT-IN (ota_erase): base64 errors keep the session alive and print no ACK/NAK marker", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_base64_errors(bench):
    """A host that waits only for '[OTA:' hangs to its per-chunk timeout on these (the Wizard's is 6 s, app.js:1733)."""
    w = usb_wcb(bench)
    img = _image(w)
    with _local(w):
        _begin(w, 4096)
        big = _data(w, 0, img[0:1025])                        # decodes to 1025 B > the 1024-B scratch buffer
        junk = _ota(w.run("?OTALOCAL,DATA,0,@@@@"))
        active = _local_status(w)
        ok = _data(w, 0, img[0:1024])
    assert big == ["[OTA] DATA base64 error -42 (chunk too big? max 1024 B decoded)"], big
    assert junk == ["[OTA] DATA base64 error -44 (chunk too big? max 1024 B decoded)"], junk
    assert active["session"] == "ACTIVE id=1  0 / 4096 B", active["session"]
    assert ok == ["[OTA:ACK,1024]"], ok


@test("ota.local_idle_timeout_nak_no_refresh", "OPT-IN (ota_erase): the 30 s idle reaper fires even while rejected chunks keep arriving (~35 s)", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_idle_timeout_nak_no_refresh(bench):
    """The offset check returns before lastActivityMs is refreshed (WCB_OTA.cpp:148 vs 165): a NAK is no keep-alive."""
    w = usb_wcb(bench)
    with _local(w):
        m0 = w.dev.mark()
        _begin(w, 4096)
        t0 = _line_time(w.dev, m0, r"^\[OTA:BEGIN,OK,0\]")
        naks = []
        for at in (10, 20):
            time.sleep(max(0.0, t0 + at - time.monotonic()))
            naks.append(_ota(w.run("?OTALOCAL,DATA,4096,AAAA")))
        w.dev.expect(r"^\[OTA\] aborted: session timed out \(current app intact\)$", timeout=max(1.0, t0 + 35 - time.monotonic()), since=m0)
        fired = _line_time(w.dev, m0, r"^\[OTA\] aborted: session timed out") - t0
        after = _local_status(w)
    bench.note(f"idle reaper fired {fired:.1f} s after BEGIN")
    assert all(n == ["[OTA] DATA rejected at offset 4096 (write cursor at 0)", "[OTA:NAK,0]"] for n in naks), naks
    assert 29.5 <= fired <= 33, f"reaper at {fired:.1f} s (a NAK must not refresh the window)"
    assert after["session"] == "idle", after


@test("ota.local_write_refreshes_timeout", "OPT-IN (ota_erase): an accepted chunk restarts the 30 s idle window (~55 s)", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_write_refreshes_timeout(bench):
    w = usb_wcb(bench)
    img = _image(w)
    with _local(w):
        m0 = w.dev.mark()
        _begin(w, 4096)
        t0 = _line_time(w.dev, m0, r"^\[OTA:BEGIN,OK,0\]")
        time.sleep(max(0.0, t0 + 20 - time.monotonic()))
        m1 = w.dev.mark()
        w.send(f"?OTALOCAL,DATA,0,{_b64(img[0:1024])}")
        w.dev.expect(r"^\[OTA:ACK,1024\]$", timeout=3, since=m1)
        t1 = _line_time(w.dev, m1, r"^\[OTA:ACK,1024\]")
        try:
            w.dev.expect(r"^\[OTA\] aborted: session timed out", timeout=max(1.0, t1 + 34 - time.monotonic()), since=m0)
            fired = _line_time(w.dev, m0, r"^\[OTA\] aborted: session timed out") - t1
        except AssertionError:
            fired = None
    bench.note(f"idle reaper fired {fired} s after the accepted chunk")
    assert fired is not None and 29.5 <= fired <= 33, f"reaper {fired} s after the accepted chunk (expected 29.5-33)"


# ============================================================ OPT-IN ota_erase: the USB baud bump
def _bump(w, rate=921600):
    """?OTALOCAL,BAUD handshake. The ACK arrives at the old rate; the board flushes, waits 20 ms and switches
    (WCB_OTA.cpp:257-260), so the host follows 100 ms later. dev.send, never run(): its echo would cross the switch."""
    m = w.dev.mark()
    w.dev.send(f"?OTALOCAL,BAUD,{rate}")
    w.dev.expect(rf"^\[OTA:BAUD,OK,{rate}\]$", timeout=3, since=m)
    time.sleep(0.1)
    w.dev.set_baud(rate)
    time.sleep(0.1)


@test("ota.local_baud_bump_abort", "OPT-IN (ota_erase): the BAUD 921600 handshake, commands at the raised rate, and ABORT restoring 115200", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_baud_bump_abort(bench):
    w = usb_wcb(bench)
    with _local(w):
        _begin(w, 4096)
        _bump(w)
        fast = w.version()
        active = _local_status(w)
        m = w.dev.mark()
        w.dev.send("?OTALOCAL,ABORT")
        w.dev.expect(r"^\[OTA\] aborted: local abort command \(current app intact\)$", timeout=3, since=m)   # printed before the switch
        time.sleep(0.15)
        w.dev.set_baud(115200)
        time.sleep(0.1)
        slow = w.version()
    assert fast == slow, f"?VERSION at 921600 {fast!r}, at 115200 {slow!r}"
    assert active["session"] == "ACTIVE id=1  0 / 4096 B", active["session"]


@test("ota.local_baud_invalid", "OPT-IN (ota_erase): BAUD accepts only 115200/230400/460800/921600/1000000; the error marker always names 115200", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_baud_invalid(bench):
    w = usb_wcb(bench)
    with _local(w):
        _begin(w, 4096)
        e1, e2 = _ota(w.run("?OTALOCAL,BAUD,57600")), _ota(w.run("?OTALOCAL,BAUD,abc"))
        m = w.dev.mark()
        w.dev.send("?OTALOCAL,BAUD,115200")          # re-applies 115200 after 20 ms: no echo sent across it
        ok = w.dev.expect(r"^\[OTA:BAUD,(OK|ERR),115200\]$", timeout=3, since=m).group(0)
        time.sleep(0.3)
        active = _local_status(w)
    assert e1 == ["[OTA:BAUD,ERR,115200]"] and e2 == ["[OTA:BAUD,ERR,115200]"], (e1, e2)
    assert ok == "[OTA:BAUD,OK,115200]", ok
    assert active["session"] == "ACTIVE id=1  0 / 4096 B", "BAUD touched the session"


@test("ota.local_baud_timeout_restore", "OPT-IN (ota_erase): the idle timeout at a raised baud restores the board to 115200; BAUD does not refresh the window (~35 s)", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_baud_timeout_restore(bench):
    w = usb_wcb(bench)
    with _local(w):
        m0 = w.dev.mark()
        _begin(w, 4096)
        t0 = _line_time(w.dev, m0, r"^\[OTA:BEGIN,OK,0\]")
        _bump(w)
        w.dev.expect(r"^\[OTA\] aborted: session timed out \(current app intact\)$", timeout=max(1.0, t0 + 35 - time.monotonic()), since=m0)
        fired = _line_time(w.dev, m0, r"^\[OTA\] aborted: session timed out") - t0
        time.sleep(0.15)
        w.dev.set_baud(115200)
        time.sleep(0.1)
        version = w.version()
    bench.note(f"timed out {fired:.1f} s after BEGIN at 921600; W1 answered {version} at 115200")
    assert 29.5 <= fired <= 33, f"timeout {fired:.1f} s after BEGIN (the window runs from BEGIN, not from BAUD)"


@test("ota.local_baud_rejected_rebegin_restores", "(should) OPT-IN (ota_erase): a rejected re-BEGIN at a raised baud restores USB to 115200 instead of stranding it with no session", needs=["wcb1"], links=[], opt_in="ota_erase")
def local_baud_rejected_rebegin_restores(bench):
    """Tracker #61. otaBegin tears a live session down before its guards run, so a rejected re-BEGIN leaves no session,
    and neither the ota.active-gated idle reaper nor BAUD can undo a raised rate. The BEGIN handler restores 115200
    itself, after the [OTA:BEGIN,ERR] marker has gone out at the raised rate. The stranded state is probed first, at
    921600; if the fix holds, that probe arrives at 115200 as line-less junk, which the empty line below flushes."""
    w = usb_wcb(bench)
    with _local(w):
        _begin(w, 4096)
        _bump(w)
        m = w.dev.mark()
        w.dev.send("?OTALOCAL,BEGIN,4096,1")
        w.dev.expect(r"^\[OTA:BEGIN,ERR,0\]$", timeout=5, since=m)
        time.sleep(0.3)
        try:
            w.version()                              # host still at 921600
            stranded = True
        except AssertionError:
            stranded = False
        if stranded:
            w.dev.send("?OTALOCAL,ABORT")            # prints nothing when idle, but always restores the rate
            time.sleep(0.2)
        w.dev.set_baud(115200)
        time.sleep(0.1)
        # A 921600 probe reaches a board already back at 115200 as a few bytes with no CR/LF, and
        # processIncomingSerial would prefix them to the next ?VERSION, which then runs as text. End that line first.
        w.dev.send("")
        time.sleep(0.2)
        w.version()
    assert not stranded, "W1 stayed at 921600 with no session after the rejected re-BEGIN"


# ============================================================ OPT-IN ota_erase: relay sessions on W2
def _rdata(offset, data, session, target=2):
    b = _b64(data)
    return f"?OTA,DATA,{target},{session},{offset}:{_crc(f'{offset},{b}')},{b}"


@contextmanager
def _relay_session(w, session, target=2):
    try:
        yield
    finally:
        w.send(f"?OTA,ABORT,{target},{session}")
        time.sleep(1.0)


@test("ota.relay_cursor_dup_gap_wrong_session_abort", "OPT-IN (ota_erase): relay session on W2: cursor ACKs, duplicate and gap re-ACK, a wrong session gets ERR with the real cursor, ABORT ignores the id", needs=["wcb1"], links=[], opt_in="ota_erase")
def relay_cursor_dup_gap_wrong_session_abort(bench):
    w = usb_wcb(bench)
    img = _image(w)
    s = _session_id()
    with Console(bench, 2) as c2, _relay_session(w, s):
        nxt2 = _status(_crun(c2, "?OTALOCAL,STATUS"))["next"]
        cm = c2.mark()
        acks = [_relay(w, f"?OTA,BEGIN,2,{s},2048,0", 2, s, timeout=8), _relay(w, _rdata(0, img[0:192], s), 2, s),
                _relay(w, _rdata(0, img[0:192], s), 2, s), _relay(w, _rdata(576, img[576:768], s), 2, s)]
        wrong = _relay(w, _rdata(192, img[192:384], s + 1), 2, s + 1)
        abort_other = _relay(w, f"?OTA,ABORT,2,{s + 1}", 2, s + 1)
        late = _relay(w, _rdata(192, img[192:384], s), 2, s)
        time.sleep(0.5)
        lines = c2.lines(cm)
    assert acks == [(0, 0), (192, 0), (192, 0), (192, 0)], f"BEGIN, DATA, duplicate, gap: {acks}"
    assert wrong == (192, 1), f"a wrong session id got {wrong} (expected ERR with the real cursor)"
    assert abort_other == (0, 0) and _has(lines, "[OTA] aborted: remote abort (current app intact)"), "an ABORT with another id did not kill the session"
    assert late == (0, 1), f"DATA after the abort got {late}"
    assert _has(lines, f"[OTA] BEGIN ok: session {s}, 2048 B → partition '{nxt2}'"), "no BEGIN ok line on W2"


@test("ota.relay_teardown_frame_err", "(should) OPT-IN (ota_erase): the relay DATA frame that tears W2's session down is ACKed ERR, not OK", needs=["wcb1"], links=[], opt_in="ota_erase")
def relay_teardown_frame_err(bench):
    """Tracker #70. A relay DATA ACK's status is read AFTER otaWrite (WCB_OTA.cpp handleOtaDataPacket), so the frame
    whose own write ends the session (here an overrun of the declared 100 B) is ACKed ERR with offset 0, as
    docs/WCB_OTA_TECHNICAL.md §4 and §8 say. Before the fix only the frames after it got ERR."""
    w = usb_wcb(bench)
    img = _image(w)
    s = _session_id()
    with Console(bench, 2) as c2, _relay_session(w, s):
        cm = c2.mark()
        begin = _relay(w, f"?OTA,BEGIN,2,{s},100,0", 2, s, timeout=8)
        tear = _relay(w, _rdata(0, img[0:192], s), 2, s)
        resend = _relay(w, _rdata(0, img[0:192], s), 2, s)
        time.sleep(0.5)
        lines = c2.lines(cm)
    assert begin == (0, 0) and resend == (0, 1), (begin, resend)
    assert _has(lines, "[OTA] write overruns image (0 + 192 > 100) — aborting"), "no overrun line on W2"
    assert tear == (0, 1), f"the frame that tore the session down was ACKed {tear}, not ERR with offset 0"


@test("ota.relay_end_incomplete_and_verify_fail", "OPT-IN (ota_erase): relay END on W2: incomplete is refused, complete-but-truncated fails verify; END ACK offset is 0; no reboot", needs=["wcb1"], links=[], opt_in="ota_erase")
def relay_end_incomplete_and_verify_fail(bench):
    w = usb_wcb(bench)
    img = _image(w)
    s1, s2 = _session_id(), _session_id()
    with Console(bench, 2) as c2, _relay_session(w, s1), _relay_session(w, s2):
        cm = c2.mark()
        _relay(w, f"?OTA,BEGIN,2,{s1},384,0", 2, s1, timeout=8)
        _relay(w, _rdata(0, img[0:192], s1), 2, s1)
        end1 = _relay(w, f"?OTA,END,2,{s1}", 2, s1, timeout=12)
        _relay(w, f"?OTA,BEGIN,2,{s2},384,0", 2, s2, timeout=8)
        d = [_relay(w, _rdata(0, img[0:192], s2), 2, s2), _relay(w, _rdata(192, img[192:384], s2), 2, s2)]
        end2 = _relay(w, f"?OTA,END,2,{s2}", 2, s2, timeout=12)
        wm = w.dev.mark()
        time.sleep(10)
        lines, rebooted = c2.lines(cm), _has(w.dev.since(wm), "[ETM] WCB2 came ONLINE (boot)")
    assert end1 == (0, 1) and _has(lines, "[OTA] END rejected: incomplete 192 / 384 B"), end1
    assert d == [(192, 0), (384, 0)] and _has(lines, "[OTA] 384 / 384 B (100%)"), d
    assert end2 == (0, 1) and any(re.search(r"\[OTA\] END verify FAILED: (\S+) \(image rejected, current app intact\)$", x) for x in lines), end2
    assert not rebooted, "W2 rebooted after a failed END"


@test("ota.relay_timeout_keepalive", "OPT-IN (ota_erase): in-session no-op relay frames keep W2's 30 s timeout alive; the timeout itself sends no ACK (~80 s)", needs=["wcb1"], links=[], opt_in="ota_erase")
def relay_timeout_keepalive(bench):
    """The local path has no such keep-alive (ota.local_idle_timeout_nak_no_refresh): in-session relay frames refresh
    lastActivityMs before the offset check (WCB_OTA.cpp:402-403)."""
    w = usb_wcb(bench)
    img = _image(w)
    s = _session_id()
    with Console(bench, 2) as c2, _relay_session(w, s):
        cm, m0 = c2.mark(), w.dev.mark()
        _relay(w, f"?OTA,BEGIN,2,{s},4096,0", 2, s, timeout=8)
        t0 = _line_time(w.dev, m0, rf"^\[OTA:ACK,2,{s},0,0\]")
        gaps = []
        for at in (10, 20, 30, 40):
            time.sleep(max(0.0, t0 + at - time.monotonic()))
            mg = w.dev.mark()
            gaps.append(_relay(w, _rdata(384, img[384:576], s), 2, s))
        t40 = _line_time(w.dev, mg, rf"^\[OTA:ACK,2,{s},")
        c2.expect(r"\[OTA\] aborted: session timed out \(current app intact\)", timeout=max(1.0, t40 + 35 - time.monotonic()), since=cm)
        fired = _line_time(c2.dev, cm, r"\[OTA\] aborted: session timed out") - t40
        acks_since_last_gap = [x for x in w.dev.since(mg) if x.startswith(f"[OTA:ACK,2,{s},")]
        final = _relay(w, _rdata(0, img[0:192], s), 2, s)
    bench.note(f"W2 timed out {fired:.1f} s after the last keep-alive")
    assert gaps == [(0, 0)] * 4, f"keep-alive frames got {gaps} (the 40 s one must still be in session)"
    assert 29.5 <= fired <= 34, f"timeout {fired:.1f} s after the last keep-alive"
    assert len(acks_since_last_gap) == 1, f"the timeout sent an ACK: {acks_since_last_gap}"
    assert final == (0, 1), final


@test("ota.cross_transport_remote_abort_kills_local", "OPT-IN (ota_erase): an ?OTA,ABORT relayed by W2 (any session id) kills W1's local USB session", needs=["wcb1"], links=[], opt_in="ota_erase")
def cross_transport_remote_abort_kills_local(bench):
    """One session per board across transports: the relay abort handler never checks the id (WCB_OTA.cpp:433-440), and
    W2 runs a mesh-delivered ?OTA (WCB.ino:5387-5393). Any board with the mesh password can cancel a USB OTA."""
    w = usb_wcb(bench)
    img = _image(w)
    with _local(w), Console(bench, 2) as c2:
        _begin(w, 4096)
        first = _data(w, 0, img[0:1024])
        cm, m = c2.mark(), w.dev.mark()
        w.send(";W2,?OTA,ABORT,1,77")
        w.dev.expect(r"^\[OTA\] aborted: remote abort \(current app intact\)$", timeout=6, since=m)
        time.sleep(3)
        w2_ack = any(x.rstrip().endswith("[OTA:ACK,1,77,0,0]") for x in c2.lines(cm))
        idle = _local_status(w)["session"] == "idle"
        after = _data(w, 1024, img[1024:2048])
    bench.note(f"W2 printed W1's abort ACK: {w2_ack}")
    assert first == ["[OTA:ACK,1024]"], first
    assert idle, "W1's session survived the remote abort"
    assert after == ["[OTA] DATA rejected at offset 1024 (write cursor at 0)", "[OTA:NAK,0]"], after


# ============================================================ OPT-IN full-image transfers
LEAK = re.compile(rb"OTA|[A-Za-z0-9+/]{32,}")          # OTA text or a base64 run leaking onto a serial port


def _progress_lines(size):
    return size // 65536 + (1 if size % 65536 else 0)     # every 65536 B plus the final one (WCB_OTA.cpp:167-171)


def _stream_local(w, image, baud=921600):
    """BEGIN (full erase, up to 30 s), the BAUD bump, ACK-paced 1024-B chunks following the cursor, the RX-overflow
    probe line, then END. Returns (mark before END, progress lines, overflow suffix of the probe line, seconds)."""
    started = time.monotonic()
    m0 = w.dev.mark()
    w.dev.send(f"?OTALOCAL,BEGIN,{len(image)},0")
    w.dev.expect(r"^\[OTA:BEGIN,OK,0\]$", timeout=30, since=m0)
    if baud != 115200:
        _bump(w, baud)
    cursor, stalls = 0, 0
    while cursor < len(image):
        m = w.dev.mark()
        w.dev.send(f"?OTALOCAL,DATA,{cursor},{_b64(image[cursor:cursor + 1024])}")
        try:
            g = w.dev.expect(r"^\[OTA:(ACK|NAK),(\d+)\]$", timeout=6, since=m)
        except AssertionError:
            g = None
        if g is None or int(g.group(2)) <= cursor:
            stalls += 1
            if stalls > 5:
                raise AssertionError(f"the transfer stalled at offset {cursor}")
            cursor = int(g.group(2)) if g else cursor
            continue
        stalls, cursor = 0, int(g.group(2))
    m = w.dev.mark()
    w.dev.send("?OTA,DATA,2,1,0:DEADBEEF,AAAA")          # CRC mismatch: dropped on W1, nothing reaches the mesh
    overflow = w.dev.expect(r"relay DATA @0 DROPPED: crc 0BC0B3E6 != DEADBEEF \(b64 4 chars\)(.*)$", timeout=3, since=m).group(1)
    m_end = w.dev.mark()
    w.dev.send("?OTALOCAL,END")
    w.dev.expect(r"^\[OTA:END,", timeout=15, since=m_end)
    time.sleep(0.3)
    progress = [x for x in w.dev.since(m0) if re.match(r"^\[OTA\] \d+ / \d+ B \(\d+%\)$", x)]
    return m_end, progress, overflow, time.monotonic() - started


@test("ota.local_sha_corrupt_full", "OPT-IN (ota_full): the full image with one flipped byte streams to 100 % and fails verify at END; no reboot, no slot change, nothing leaks onto S2-S5", needs=["wcb1"], links=["W1S2", "W1S3", "W1S4", "W1S5"], opt_in="ota_full")
def local_sha_corrupt_full(bench):
    w = usb_wcb(bench)
    img = _image(w)
    corrupt = bytearray(img)
    corrupt[700000] ^= 0xFF
    wires = [l for l in (bench.links.get(1, p) for p in ("S2", "S3", "S4", "S5")) if l]
    watch = Watch(*wires)
    with _local(w):
        before = _local_status(w)
        m_end, progress, overflow, secs = _stream_local(w, bytes(corrupt))
        end = _ota(w.dev.since(m_end))
        time.sleep(0.15)
        w.dev.set_baud(115200)                     # after END,ERR the board restores 115200 itself
        time.sleep(6)
        rebooted = _has(w.dev.since(m_end), "Reset reason:")
        after = _local_status(w)
    leaks = [l.key for l in wires if LEAK.search(watch.got(l))]
    bench.note(f"corrupt full image streamed in {secs:.0f} s")
    assert len(progress) == _progress_lines(len(img)) and progress[-1] == f"[OTA] {len(img)} / {len(img)} B (100%)", progress[-2:]
    assert not overflow, f"serial RX overflowed during the transfer: {overflow!r}"
    assert any(re.match(r"^\[OTA\] END verify FAILED: (\S+) \(image rejected, current app intact\)$", x) for x in end), end
    assert "[OTA:END,ERR]" in end and "[OTA:END,OK]" not in end, end
    assert not rebooted, "W1 rebooted after a failed END"
    assert after["running"] == before["running"] and after["session"] == "idle", after
    assert not leaks, f"OTA traffic leaked onto {leaks}"


@test("ota.local_full_same_image_wcb1", "OPT-IN (ota_full): re-flash W1 over USB with the image it runs, twice; each pass reboots into the other slot with the same version and config (2 reboots)", needs=["wcb1"], links=[], opt_in="ota_full", opt_in_why="erases and rewrites W1's inactive app slot and switches its boot slot twice")
def local_full_same_image_wcb1(bench):
    """END OK switches otadata, which ?backup cannot see, so there are always two passes and W1 ends on its original slot.
    Rollback only rescues an image that dies before initArduino (esp32-hal-misc.c:277-292). If W1 does not come back, the
    recovery is a USB reflash by the operator: the harness never runs esptool."""
    w = usb_wcb(bench)
    img, fw = _image(w), w.version()
    passes = []
    with config_guard(bench, 1):
        start = _local_status(w)
        try:
            for _ in range(2):
                before = _local_status(w)
                m_end, progress, overflow, secs = _stream_local(w, img)
                end = _ota(w.dev.since(m_end))
                w.dev.set_baud(115200)                 # the restart comes 2 s after [OTA:END,OK], at 115200
                w.wait_boot(m_end, timeout=25)
                passes.append(dict(before=before, end=end, boot=w.dev.since(m_end), after=_local_status(w),
                                   version=w.version(), overflow=overflow, secs=secs))
        finally:
            _recover_local(w)
    bench.note("full local OTA passes: " + "; ".join(f"{p['secs']:.0f} s {p['before']['running']}->{p['after']['running']}" for p in passes))
    for n, p in enumerate(passes, 1):
        nxt = p["before"]["next"]
        assert _has(p["end"], f"[OTA] END ok: verified {len(img)} B → next boot '{nxt}'") and "[OTA:END,OK]" in p["end"], f"pass {n} END: {p['end']}"
        assert _has(p["boot"], "Reset reason: 3 - Software Reset") and _has(p["boot"], f"Software Version: {fw}"), f"pass {n} boot banner"
        assert p["after"]["running"] == nxt and p["after"]["next"] == p["before"]["running"] and p["after"]["session"] == "idle", f"pass {n}: {p['after']}"
        assert p["version"] == fw and not p["overflow"], f"pass {n}: version {p['version']}, overflow {p['overflow']!r}"
    assert len(passes) == 2 and passes[-1]["after"]["running"] == start["running"], "W1 did not end on its original slot"


@test("ota.local_wrong_chip_image", "OPT-IN (ota_wrong_chip, attended): an ESP32-S3 image declared as family 0 passes BEGIN and must fail verify at END", needs=["wcb1"], links=[], opt_in="ota_wrong_chip")
def local_wrong_chip_image(bench):
    """Design gap vs docs/WCB_OTA_TECHNICAL.md:288 ('chip-family mismatch -> BEGIN rejected'): the brick guard compares
    only the host-declared family (WCB_OTA.cpp:108-113), never the image header's chip id, so only IDF verify at END
    stops a wrong image with a correct declared family. If END ever succeeded W1 would reboot into an S3 image: stop
    and reflash it by USB."""
    w = usb_wcb(bench)
    s3_image = _image(w, chip="ESP32S3")
    with _local(w):
        before = _local_status(w)
        m_end, _, _, _ = _stream_local(w, s3_image)
        end = _ota(w.dev.since(m_end))
        time.sleep(0.15)
        w.dev.set_baud(115200)
        time.sleep(6)
        rebooted = _has(w.dev.since(m_end), "Reset reason:")
        after = _local_status(w)
    assert "[OTA:END,ERR]" in end and "[OTA:END,OK]" not in end, f"END: {end}"
    assert _has(end, "END verify FAILED: ESP_ERR_OTA_VALIDATE_FAILED"), end
    assert not rebooted and after["running"] == before["running"], after


@test("ota.relay_full_same_image_wcb2", "OPT-IN (ota_full_wcb2): relay-OTA W2 with the image it runs, twice; each pass reboots W2 into the other slot with the same version and config (slow: ~7000 frames per pass at 115200)", needs=["wcb1"], links=[], opt_in="ota_full_wcb2")   # the W2S1 tap is used when wired, never required
def relay_full_same_image_wcb2(bench):
    """Run only after ota.local_full_same_image_wcb1 passed with the same file (one ESP32 image serves HW 1.0 and 2.4;
    pin maps are runtime). The relay has no baud bump (?OTALOCAL,BAUD needs a local session, WCB_OTA.cpp:247). W2
    sends its END ACK once, 300 ms before restarting (WCB_OTA.cpp:425-429), so its boot line also counts as success.
    W2's own USB cable is the recovery path if the new image fails to run."""
    w = usb_wcb(bench)
    img = _image(w)
    tap = bench.links.get(2, "S1")
    passes = []
    with config_guard(bench, 2), Console(bench, 2) as c2:
        for _ in range(2):
            before = _status(_crun(c2, "?OTALOCAL,STATUS"))
            s = _session_id()
            tm = tap.mark() if tap else None
            started = time.monotonic()
            try:
                if _relay(w, f"?OTA,BEGIN,2,{s},{len(img)},0", 2, s, timeout=30) != (0, 0):
                    raise AssertionError("W2 refused the full-size BEGIN")
                cursor, stalls = 0, 0
                while cursor < len(img):
                    try:
                        offset, status = _relay(w, _rdata(cursor, img[cursor:cursor + 192], s), 2, s, timeout=4)
                    except AssertionError:
                        offset, status = cursor, 0
                    if status != 0 or (offset == 0 and cursor > 192):
                        raise AssertionError(f"W2's session failed at offset {cursor} (ACK {offset},{status})")
                    if offset <= cursor:
                        stalls += 1
                        if stalls > 60:
                            raise AssertionError(f"the relay stalled at offset {cursor}")
                        time.sleep(min(stalls * 0.05, 0.6))       # the Wizard's backoff (app.js:1910-1945)
                        continue
                    stalls, cursor = 0, offset
                m = w.dev.mark()
                try:
                    end = _relay(w, f"?OTA,END,2,{s}", 2, s, timeout=12)
                except AssertionError:
                    end = None
                w.dev.expect(r"\[ETM\] WCB2 came ONLINE \(boot\)", timeout=25, since=m)
            except AssertionError:
                w.send(f"?OTA,ABORT,2,{s}")
                raise
            time.sleep(4)
            after = _status(_crun(c2, "?OTALOCAL,STATUS"))
            passes.append(dict(before=before, after=after, end=end, secs=time.monotonic() - started,
                               leaked=bool(tap and LEAK.search(tap.received(tm)))))
    bench.note("relay OTA passes to W2: " + "; ".join(f"{p['secs']:.0f} s, END ACK {p['end']}" for p in passes))
    for n, p in enumerate(passes, 1):
        assert p["end"] in (None, (0, 0)), f"pass {n} END ACK {p['end']}"
        assert p["after"]["running"] == p["before"]["next"] and p["after"]["firmware"] == p["before"]["firmware"], f"pass {n}: {p['after']}"
        assert p["after"]["session"] == "idle" and not p["leaked"], f"pass {n}: session {p['after']['session']}, leak {p['leaked']}"
    assert passes[-1]["after"]["running"] == passes[0]["before"]["running"], "W2 did not end on its original slot"
