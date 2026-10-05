"""NaviCore's own pins on a wire (docs/hil_plan/NAVICORE.md NC-WP14, ids ncwire.*): its aux ports S3-S5, SBUS OUT and its
local Maestro bus, read - and S3-S5 also driven - by a third probe wired as D-NC37 has it.

Every test is opt-in navicore_wire (hil/optin.py) and names the wires it needs in links=[...]: N20S3, N20S4 and N20S5
(the aux ports, duplex), N20SBO (SBUS OUT) and N20MAE (the Maestro bus), the last two listen-only taps (hil/links.py
NcLink; docs/HIL_TESTING.md §2 "NaviCore's own pins"). So each one skips, saying why, until probe 3 is wired, run.py
--discover has found those wires and the opt-in is ticked. Nothing here has run on the bench yet (written 2026-10-05,
before the probe was wired).

Line numbers are NaviCore main 639e2e7 (with the D-NC fixes of 2026-10-05, NAVICORE.md §7.2), WcbCmd 0.9.1 and the stock
EspSoftwareSerial 8.1.0 that NaviCore compiles from the Arduino-Code sketchbook.

Where the bytes come from:
- S3 is the hardware UART0 on the NaviCore v2 profile; S4 and S5 are bit-banged EspSoftwareSerial (NaviCore.ino:242-269,
  :4825-4836). Each opens at GET_CONFIG's auxBaud, 8N1 (auxBegin :282-286, applySerialBauds :3429-3465), on the v2 pins
  S3 TX8/RX9, S4 TX10/RX21, S5 TX38/RX47 (:178-187). A test binds each wire at the baud GET_CONFIG gives now
  (hil/links.py nc_bauds), never a fixed one.
- Text reaches those ports through one paced transmitter: a serial action (queueSerialAction :2151-2164, RA_SERIAL
  :2220-2240) and a mesh ;W20,;s<n> forward (:3260-3277; mesh port n is S<n+2>, rc_config.h:2042-2044) wait in
  serialFwdQueue (4 deep, NaviCore.ino:5036), and auxTxPump hands each port a few bytes a loop() pass - one at 9600 on a
  bit-banged port - with a CR after the text (:5216-5300). A device write - an HCR action (:1822), a fade step (:5796),
  #L20/#L21 (:3895-3908), an MP3 Trigger, DFPlayer or WLED - goes straight to the port, whatever the pump is doing.
- SBUS OUT is UART1's TX (GPIO5): while sbusOutEnabled is on, every byte NaviCore reads off SBUS IN is written to it as
  it is read (sbus_reader.h:89-93; applySbusOut NaviCore.ino:3477-3498, :4836-4846). The probe reads it on a hardware
  channel at 100000 8E2, inverted, RX only.
- The Maestro bus is Serial2's TX (GPIO6) at auxBaud.maestro (:3434-3440). A ?MAE query on a local slot writes one
  Pololu frame there and reads the reply off its RX within 25 ms (maestroWrite :692-735, maestroLocalQuery :770-787, the
  CLI :3589-3631).
- A NAVICORE_HIL_HOOKS image also logs each block it hands S3, S4, S5 or Serial2 as '[WIRE] <port> <off>/<len>: <hex>'
  under DBG_WIRE (navicore_hil.h:45-75, :106). Where a test has both, the wire is the truth and the log is checked
  against it: it is the instrument s44 reads NaviCore's own ports through.

Probe channels (docs/HIL_TESTING.md §2 "Channels and baud"): headers S1 (SBUS OUT) and S2 (S3) take a hardware channel
only, and the Maestro bus at 57600 needs one too, so no test binds more than two of those three. soft_tx_integrity wants
framing errors reported on S4 and S5, which only a hardware UART does (EspSoftwareSerial reports none,
wcb_probe/probe_main.cpp:400-430), so it binds those two on the hardware channels and nothing else.

What is written, and what is put back:
- Plain text out S3-S5, the kind navicore.serial_route_dbg sends every run. A test skips while an HCR, MP3 Trigger,
  DFPlayer or WLED is routed to a port it uses (s44 _ports_free): that device's own traffic would land in the middle of
  the test's.
- Lines typed into S3-S5 by the probe: NaviCore reads them (its RX monitor, NaviCore.ino:3927-3988), and only where a
  test saved serialBcast in does it broadcast them.
- Config writes go inside nc_guard (hil/nc_guard.py), which proves the config byte-identical afterwards:
  rx_monitor_bcast_in's serialBcast flags, and with navicore_aux_tx the HCR route of aux_bytes and tx_interleave.
- Device-protocol bytes out NaviCore's S4 (#L21, an HCR action, a fade) only with navicore_aux_tx ticked too, as in s44
  (D-NC14); a NaviCore restart only with navicore_reboot ticked too (s3_console_quiet, listed in hil/servos.py).
- Nothing moves: the Maestro bus test sends reads only, and no test touches the SBUS controller.
- Every wire is released when its test ends (the runner does it too): a channel left on SBUS OUT would log ~111 probe
  lines a second.

Messages quote markers, byte counts and this test's own bytes; anything read off NaviCore's console or the S3 wire goes
through redact_text first (the boot banner names the SoftAP).
"""
import re
import statistics
import threading
import time

from hil import optin
from hil.checkpoint import redact_text
from hil.links import nc_bauds
from hil.navicore import DBG_HCR, DBG_SERIAL, SBUS_FULL_FPS, NaviCore, parse_mae
from hil.nc_guard import nc_guard
from hil.runner import Skip, test
from hil.sbus import decode as sbus_decode
from suites.common import Watch, nonce, padded, usb_wcb
from suites.s21_navicore_sbus import _expected_ports, _port_copies
from suites.s40_navicore_config import _hooks
from suites.s42_navicore_sbus_engine import DBG_WIRE
from suites.s44_navicore_devices import _ports_free, _trace, aux_label, hcr, wire_log
from suites.s46_navicore_boot import _restart

SERIAL_CMD_KEEP = 95             # RcAction.cmd[96] (rc_config.h:130): the longest serial action text
FWD_KEEP = 200                   # SerialFwdMsg.text[201] (NaviCore.ino:327): the longest mesh forward
HCR_TEST = b"<OH80,QEH>\n" * 2   # #L20/#L21: hcrFormatCommand(2, 0, 80) and the raw frame, the same bytes (:3895-3908)
FADE_STEP_MS = 150               # WcbCmd HcrFade::STEP_MS (WcbHcrFade.h:48)
SBUS_BYTE_MS = 0.12              # one byte at 100000 8E2: start, 8 data, parity, 2 stop = 12 bits
IDF_LINE = re.compile(r"^[EW] \(\d+\) [^:\s]+:")   # an ESP-IDF error or warning (CONFIG_LOG_DEFAULT_LEVEL is ERROR)
# The console burst of s3_console_quiet: output NaviCore prints on USB only, each line flushed with #L12. '?HILQ<tag>' is
# an unknown command NaviCore echoes ('Unknown command: ?HILQ...', NaviCore.ino:4027), the marker a mirrored console
# would carry onto S3.
CONSOLE_BURST = ("?version", "?WDP,DUMP", "#L09", "#L11", "#L13", "?OTALOCAL,STATUS", "?REC,INFO")

# Timing, module-level so the self-test can shorten it.
SETTLE_S = 1.5        # after a config save: easing re-applies and repeats twice 500 ms apart (s44's rule)
QUIET_S = 1.0         # a window that must stay silent
SBUS_WINDOW_S = 1.5   # how long sbus_out_tee reads SBUS OUT
LINE_PAIRS = 100      # soft_tx_integrity: lines to each of S4 and S5
PING_EVERY_S = 0.2    # soft_tx_integrity's mesh load: a bridged PING from W1 this often
MAX_LOST_LINES = 10   # soft_tx_integrity stops waiting line by line after this many never arrived
FADE_WAIT_S = 1.8     # aux_bytes: a 1 s FadeIn and its last step
MESH_WAIT_S = 1.5     # rx_monitor_bcast_in: an unacknowledged mesh broadcast reaching every WCB port
TYPED_S = 0.4         # rx_monitor_bcast_in: NaviCore printing a line the probe typed in
FRAGMENT_S = 0.5      # rx_monitor_bcast_in: past the 60 ms of quiet that flush a fragment (NaviCore.ino:3978)


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _wire(bench, port, bauds, hw=None):
    """NaviCore's <port> wire, bound at `bauds[port]` (nc_bauds of a GET_CONFIG read in this test), or Skip."""
    return bench.links.nc_require(port).listen(bauds[port], hw=hw)


def _release(*links):
    for l in links:
        try:
            l.release()
        except Exception:  # noqa: BLE001 - a probe gone since; the runner's release reports it
            pass


def _aux_tx_opted(bench):
    """Skip unless navicore_aux_tx is ticked too: for a test gated on navicore_wire that writes device bytes."""
    if "navicore_aux_tx" not in optin.enabled(bench.cfg):
        raise Skip(optin.skip_reason("navicore_aux_tx"))


def _bcast_out_off(cfg, *ports):
    """Skip while serialBcast out is on for one of `ports`: any mesh broadcast would land there mid-test."""
    on = [p for p in ports if ((cfg.get("serialBcast") or {}).get(p) or {}).get("out")]
    if on:
        raise Skip(f"serialBcast out is on for {', '.join(on)}: mesh broadcasts reach it during the test")


def _await_bytes(l, since, want, timeout):
    """The bytes wire `l` received since `since`, once each of `want` (bytes) is among them or `timeout` passed."""
    deadline = time.monotonic() + timeout
    while True:
        got = l.received(since)
        if all(w in got for w in want) or time.monotonic() >= deadline:
            return got
        time.sleep(0.02)


def _await_len(l, since, n, timeout):
    """Wait until wire `l` has at least `n` bytes since `since` -> them (fewer on a timeout)."""
    deadline = time.monotonic() + timeout
    while True:
        got = l.received(since)
        if len(got) >= n or time.monotonic() >= deadline:
            return got
        time.sleep(0.005)


def _bcast_ports(bench):
    """{link key: (link, takes a plain broadcast)} for the probe-wired W1/W2 ports (s21 _expected_ports)."""
    return _expected_ports(bench)


def _q(data, n=80):
    """Bytes for a message: repr of at most n bytes, credentials hashed."""
    text = repr(bytes(data[:n])) + ("..." if len(data) > n else "")
    return redact_text(text)


# ------------------------------------------------------------------ pure helpers (selftest.py feeds them fixtures)
def line_tally(got, lines):
    """How `lines` (str, each sent with one CR, in order) came out in `got`, the bytes a wire received -> (exact, missing,
    extra): exact counts the lines found whole exactly once, missing lists the others' indexes, extra counts the bytes no
    exact line accounts for (a damaged line's remains, a stray). Only got == the lines joined is clean."""
    exact, missing, used = 0, [], 0
    for i, text in enumerate(lines):
        w = text.encode() + b"\r"
        if got.count(w) == 1:
            exact += 1
            used += len(w)
        else:
            missing.append(i)
    return exact, missing, len(got) - used


def interleave_problem(got, line, dev):
    """None when `got` (a wire's bytes) is the paced `line` and the device write `dev` one after the other, in either
    order, each whole; else what happened, in words: the device bytes inside the line (after which byte), the two merged
    byte by byte, one of them missing, or other bytes."""
    if got in (line + dev, dev + line):
        return None
    k = got.find(dev)
    if 0 < k < len(line) and got[:k] == line[:k] and got[k + len(dev):] == line[k:]:
        return f"its {len(dev)} bytes landed inside the line, after byte {k} of {len(line)}"

    def merged(a, b, s):
        i = j = 0
        for c in s:
            if i < len(a) and a[i] == c:
                i += 1
            elif j < len(b) and b[j] == c:
                j += 1
            else:
                return False
        return i == len(a) and j == len(b)
    if len(got) == len(line) + len(dev) and merged(line, dev, got):
        return "its bytes and the line's arrived mixed together"
    if line not in got and dev not in got:
        return f"neither arrived whole: {_q(got)}"
    if dev not in got:
        return f"the device bytes never arrived whole: {_q(got)}"
    if line not in got:
        return f"the line never arrived whole: {_q(got)}"
    return f"other bytes came with them: {_q(got)}"


def frame_walk(data, frame):
    """Read `data` (a wire's bytes) as back-to-back copies of `frame` -> (offset of the first whole copy or -1, whole
    copies, problem or None). The bytes before the first copy must be the tail of a frame and the bytes after the last
    the head of one: the window opened and closed mid-frame. Anything else is named: a copy that differs (which byte),
    or stray bytes."""
    n = len(frame)
    i = data.find(frame)
    if i < 0:
        return -1, 0, "no whole copy of #L13's frame in the window"
    if i >= n or (i and data[:i] != frame[-i:]):
        return i, 0, f"{i} bytes before the first copy are not the end of a frame"
    j, count = i, 0
    while data[j:j + n] == frame:
        j += n
        count += 1
    rest = data[j:]
    if len(rest) >= n:
        k = next(x for x in range(n) if rest[x] != frame[x])
        return i, count, (f"frame {count + 1} differs from #L13's at byte {k} (0x{rest[k]:02X}, #L13 0x{frame[k]:02X}), "
                          f"after {count} whole copies")
    if rest != frame[:len(rest)]:
        return i, count, f"the {len(rest)} bytes after the last copy are not the start of a frame"
    return i, count, None


def frame_times(bursts, start, n, count):
    """Probe-clock ms of each of `count` frames of `n` bytes from byte `start` of the stream `bursts` ([(ms, bytes)]) makes:
    the ms of the burst holding the frame's first byte, plus SBUS_BYTE_MS for each byte of that burst before it."""
    starts, at = [], 0
    for ms, chunk in bursts:
        starts.append((at, ms))
        at += len(chunk)
    out = []
    for k in range(count):
        off = start + k * n
        b0, ms = [s for s in starts if s[0] <= off][-1]
        out.append(ms + (off - b0) * SBUS_BYTE_MS)
    return out


def fade_steps(bursts, ch="A"):
    """[(probe ms, level)] of every '<PV<ch><n>>' SetVolume frame in a wire's bursts, in order (HcrCodec's format, WcbCmd
    WcbHcr.cpp)."""
    stream, starts = b"", []
    for ms, chunk in bursts:
        starts.append((len(stream), ms))
        stream += chunk
    out = []
    for m in re.finditer(rb"<PV" + ch.encode() + rb"(\d+)>\n", stream):
        out.append(([t for s, t in starts if s <= m.start()][-1], int(m.group(1))))
    return out


def query_frame(q, dev, ch=0):
    """The Pololu frame a ?MAE query writes: getPosition AA <dev> 10 <ch>, getMovingState AA <dev> 13, getErrors AA
    <dev> 21 (0x90, 0x93, 0xA1 with the MSB cleared: maestroWrite, NaviCore.ino:719-735)."""
    cmd = {"pos": 0x10, "mov": 0x13, "err": 0x21}[q]
    return bytes([0xAA, dev & 0x7F, cmd]) + (bytes([ch & 0xFF]) if q == "pos" else b"")


def console_text(got, n=4):
    """The runs of at least `n` printable ASCII characters in `got`: console text, as against the noise a line left
    floating through a reset can read as."""
    return re.findall(rb"[\x20-\x7e]{%d,}" % n, bytes(got))


# ============================================================ the aux ports, both ways
@test("ncwire.aux_bytes", "NaviCore's S3, S4 and S5 transmit at GET_CONFIG's baud on their own pins: a serial action's "
      "text and a mesh ;W20,;s<n> forward's each arrive byte-exact with one CR, on that port only; on a hook image the "
      "wire equals NaviCore's DBG_WIRE copy; with navicore_aux_tx, a local HCR fade's steps reach S4 150 ms apart on the "
      "probe's clock", needs=["navicore", "wcb1"], links=["N20S3", "N20S4", "N20S5"], opt_in="navicore_wire")
def aux_bytes(bench):
    """nc.aux.binding (and the wire half of nc.hcr.fade): each aux port is the configured UART on the pins its wire
    found, at GET_CONFIG's baud (applySerialBauds, NaviCore.ino:3429-3465). A serial action (RA_SERIAL :2220-2240) and a
    targeted ;W20,;s<n> forward (:3260-3277, mesh port n = S<n+2>) both go through the paced transmitter, which ends the
    text with a CR and logs '[DISPATCH] Serial TX [<label>]  <text>' once the line is out (auxTxPump :5253-5276). Each
    must reach its own wire exactly - the text, one CR, nothing else - and leave the other two ports' wires without its
    marker. On a hook image the [WIRE] log of the port, joined (s44 wire_log), must equal what the wire got.
    With navicore_aux_tx ticked too, inside nc_guard: hcrDest local on S4, SetVolume A 60 (<PVA60>), then FadeIn A over
    1 s (executeHcrAction :1773-1783; HcrFade, WcbCmd WcbHcrFade.cpp:15-67): an anchor <PVA0>, steps every STEP_MS
    (150 ms) rising to <PVA60>, timed on the probe's clock - the step period no USB-timed copy can show."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    cfg = nc.config()
    _ports_free(cfg, "S3", "S4", "S5")
    _bcast_out_off(cfg, "S3", "S4", "S5")
    bauds, nid, hook = nc_bauds(cfg), nc.wcb_status()["self"], _hooks(nc)
    tag, problems, notes = nonce(), [], []
    wires = {p: _wire(bench, p, bauds) for p in ("S3", "S4", "S5")}
    try:
        with nc.debug(DBG_SERIAL | (DBG_WIRE if hook else 0)):
            for port, l in wires.items():
                mesh = int(port[1]) - 2
                for how, text in (("a serial action", f"HILA{tag}{port}a"), (f";W{nid},;s{mesh}", f"HILA{tag}{port}m")):
                    want = text.encode() + b"\r"
                    watch, nm = Watch(*wires.values()), nc.dev.mark()
                    if how == "a serial action":
                        ack = nc.test_action({"type": "serial", "port": port, "cmd": text})
                        if not ack.get("ok"):
                            problems.append(f"{port}, {how}: TEST_ACTION answered {ack}")
                            continue
                    else:
                        w1.send(f";W{nid},;s{mesh}{text}")
                    _await_bytes(l, watch.marks[l.key], [want], 3.0)
                    time.sleep(0.3)
                    lines = _trace(nc, nm, 0.1)
                    got = watch.got(l)
                    if got != want:
                        problems.append(f"{port}, {how}: its wire got {_q(got)}, expected {want!r} at {bauds[port]} baud")
                    stray = [o for o, ol in wires.items() if o != port and text.encode() in watch.got(ol)]
                    if stray:
                        problems.append(f"{port}, {how}: {', '.join(stray)} got its text too")
                    label = aux_label(cfg, port)
                    if f"[DISPATCH] Serial TX [{label}]  {text}" not in lines:
                        problems.append(f"{port}, {how}: no '[DISPATCH] Serial TX [{label}]  {text}' line")
                    if hook:
                        logged, gaps = wire_log(lines, port)
                        if gaps:
                            notes.append(f"{port}, {how}: the [WIRE] log lost lines ({'; '.join(gaps)})")
                        elif logged != got:
                            problems.append(f"{port}, {how}: NaviCore's [WIRE] {port} log says {_q(logged)}, the wire "
                                            f"got {_q(got)}")
        if not hook:
            notes.append("not a NAVICORE_HIL_HOOKS image: the DBG_WIRE copy was not compared with the wire")
        if "navicore_aux_tx" in optin.enabled(bench.cfg):
            problems += _fade_on_wire(bench, wires["S4"], notes)
        else:
            notes.append("navicore_aux_tx is off: the local HCR fade was not timed on the wire")
    finally:
        _release(*wires.values())
    bench.note("aux_bytes: " + "; ".join([f"bauds S3 {bauds['S3']}, S4 {bauds['S4']}, S5 {bauds['S5']}"] + notes))
    assert not problems, "; ".join(problems)


def _fade_on_wire(bench, l, notes):
    """aux_bytes' fade half (navicore_aux_tx): -> problems."""
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        nc.set_config({"hcrDest": {"transport": "serial", "port": "S4"}})
        time.sleep(SETTLE_S)
        with nc.debug(DBG_HCR):
            m = l.mark()
            nc.test_action(hcr(17, 1, 60))
            got = _await_bytes(l, m, [b"<PVA60>\n"], 2.0)
            if got != b"<PVA60>\n":
                problems.append(f"SetVolume A 60 on S4: the wire got {_q(got)}, expected b'<PVA60>\\n'")
            m = l.mark()
            nc.test_action(hcr(12, 1, 1))
            time.sleep(FADE_WAIT_S)
            steps = fade_steps(l.bursts(m))
    levels = [v for _, v in steps]
    gaps = [b[0] - a[0] for a, b in zip(steps, steps[1:])]
    notes.append(f"FadeIn A over 1 s on the wire: {levels}, {gaps} ms apart")
    if not levels or levels[0] != 0 or levels[-1] != 60 or levels != sorted(levels) or not 4 <= len(levels) <= 12:
        problems.append(f"FadeIn A over 1 s put {levels} on S4: expected 0 first, a rising ramp, 60 last")
    elif not abs(statistics.median(gaps) - FADE_STEP_MS) <= 30:
        problems.append(f"FadeIn A's steps came {statistics.median(gaps):.0f} ms apart on the probe's clock (median of "
                        f"{gaps}); HcrFade steps every {FADE_STEP_MS} ms")
    return problems


@test("ncwire.soft_tx_integrity", "(should) NaviCore's bit-banged S4 and S5 transmit byte-exact under load: 200 "
      "95-character lines, 100 to each port through the paced transmitter while SBUS streams at full rate and W1 pings "
      "NaviCore over the mesh, reach two hardware UARTs on probe 3 with no framing error and no wrong byte (D-NC24: "
      "EspSoftwareSerial 8.1.0 transmits with interrupts on)", needs=["navicore", "wcb1"], links=["N20S4", "N20S5"],
      opt_in="navicore_wire")
def soft_tx_integrity(bench):
    """NAVICORE.md D-NC24 (the map's nc.aux.soft_tx_interrupts). S4 and S5 are EspSoftwareSerial (NaviCore.ino:264-269,
    :4827-4831), begun with no enableIntTx (auxBegin :282-286), so they transmit with interrupts enabled - the library's
    default (EspSoftwareSerial 8.1.0, SoftwareSerial.cpp:92) - while NaviCore's comments say a write runs with interrupts
    off (:321, :5221-5222). An interrupt inside a bit stretches it; the WCB lost 45 % of HCR commands on its S5 that way
    under mesh load before it moved soft TX to RMT (WCB CLAUDE.md rule 13). D-NC24's recommendation is to change the
    firmware only once a wire measures it: this is that measurement.
    Load: SBUS at full rate (#L09 fps of at least SBUS_FULL_FPS, else the test skips - nothing to load it), DBG_SERIAL on
    (a console line per finished line), and W1 sending NaviCore a bridged PING every PING_EVERY_S, each answered over the
    mesh. Lines: 100 pairs of serial actions, one to S4 and one to S5, each pair sent once the previous one is on the
    wires, so the 4-deep queue never fills and loop() never waits on it (queueSerialAction :2159-2164). Both wires on a
    hardware channel at GET_CONFIG's baud, which reports a mis-timed bit as a FRAME error (RXERR). Pass: each wire got
    exactly its 100 lines, each with its CR, in order, and no RXERR. A failing run is D-NC24's evidence: the fix is RMT TX,
    as the WCB's (rule 13)."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    cfg = nc.config()
    _ports_free(cfg, "S4", "S5")
    _bcast_out_off(cfg, "S4", "S5")
    bauds, nid = nc_bauds(cfg), nc.wcb_status()["self"]
    st = nc.sbus_full_rate()
    if st["fps"] < SBUS_FULL_FPS:
        raise Skip(f"NaviCore reads SBUS at {st['fps']} fps: there is no SBUS load to transmit under")
    tag = nonce()
    lines = {p: [padded(f"T{p[1]}{i:03d}{tag}", SERIAL_CMD_KEEP) for i in range(LINE_PAIRS)] for p in ("S4", "S5")}
    line_s = max((SERIAL_CMD_KEEP + 1) * 10 / bauds[p] for p in ("S4", "S5"))
    wires = {p: _wire(bench, p, bauds, hw=True) for p in ("S4", "S5")}
    stop, pings, lost = threading.Event(), [], 0

    def mesh_load():
        while not stop.is_set():
            try:
                w1.send(f';W{nid},{{"type":"PING"}}')
                pings.append(time.monotonic())
            except Exception:  # noqa: BLE001 - a W1 hiccup must not end the measurement; the count shows it
                pass
            stop.wait(PING_EVERY_S)
    try:
        marks, wm = {p: l.mark() for p, l in wires.items()}, w1.dev.mark()
        loader = threading.Thread(target=mesh_load, daemon=True)
        with nc.debug(DBG_SERIAL):
            loader.start()
            try:
                for i in range(LINE_PAIRS):
                    pm = {p: l.mark() for p, l in wires.items()}
                    for p in ("S4", "S5"):
                        nc.test_action({"type": "serial", "port": p, "cmd": lines[p][i]})
                    if lost < MAX_LOST_LINES:
                        for p, l in wires.items():
                            want = lines[p][i].encode() + b"\r"
                            if want not in _await_bytes(l, pm[p], [want], 2 * line_s + 1.0):
                                lost += 1
                    else:
                        time.sleep(2 * line_s + 0.05)
                time.sleep(2 * line_s + 0.3)
            finally:
                stop.set()
                loader.join(timeout=2)
        got = {p: l.received(marks[p]) for p, l in wires.items()}
        errs = {p: l.errors(marks[p]) for p, l in wires.items()}
        pongs = sum(1 for x in w1.dev.since(wm) if x.startswith(f'{{"sys":1,"type":"PONG","id":{nid}'))
    finally:
        _release(*wires.values())
    after = nc.sbus_dump()
    problems, tally = [], []
    for p in ("S4", "S5"):
        exact, missing, extra = line_tally(got[p], lines[p])
        kinds = sorted({e.split()[3] for e in errs[p] if len(e.split()) > 3})
        tally.append(f"{p}: {exact}/{LINE_PAIRS} exact, {extra} other bytes, RXERR {len(errs[p])}"
                     + (f" ({', '.join(kinds)})" if kinds else ""))
        if got[p] != b"".join(x.encode() + b"\r" for x in lines[p]) or errs[p]:
            problems.append(tally[-1] + (f", first damaged line {missing[0] + 1}" if missing else ""))
    bench.note(f"soft_tx_integrity at {bauds['S4']}/{bauds['S5']} baud: {'; '.join(tally)}; SBUS {st['fps']} fps "
               f"before, {after['fps']} after; {len(pings)} bridged PINGs, {pongs} PONGs back on W1")
    assert not problems, (f"(should, D-NC24) bit-banged TX lost bytes under load: {'; '.join(problems)}. "
                          f"EspSoftwareSerial transmits with interrupts on; RMT TX, as the WCB's (rule 13), is the fix")


@test("ncwire.s3_console_quiet", "(should) NaviCore's console never reaches S3's TX, the pins it gives UART0, which the "
      "ESP-IDF console also uses: nothing on the S3 wire while NaviCore prints a burst of USB-only output, nor across a "
      "restart (with navicore_reboot), and no IDF log line NaviCore printed meanwhile (the map's "
      "nc.aux.uart0_console_leak)", needs=["navicore"], links=["N20S3"], opt_in="navicore_wire")
def s3_console_quiet(bench):
    """The map's nc.aux.uart0_console_leak ('plausible'). On the v2 profile S3 is the hardware UART0, begun on GPIO8/9
    (s3 = &Serial0, NaviCore.ino:4826; auxBegin :282-286), and the core 3.3.4 build NaviCore compiles keeps UART0 as the
    ESP-IDF console (esp32-arduino-libs esp32s3/sdkconfig: CONFIG_ESP_CONSOLE_UART_NUM=0, the USB-Serial/JTAG only the
    secondary console; CONFIG_LOG_DEFAULT_LEVEL 1, errors). So once Serial0 has moved UART0's TX to GPIO8, an IDF error
    line - 'E (<ms>) <tag>: ...', which NaviCore's USB also shows - goes out S3 at S3's baud, into whatever is wired
    there (an HCR at 115200 reads it as a command stream). Seen on NaviCore's USB on this bench only around WiFi station
    churn ('E (n) wifi:CCMP replay detected', 'wifi:addba response cb: ap bss deleted', runs 20260929-114920 and
    -122112); no hook makes one on demand (navicore_hil.h has #L90-#L93, none of them an IDF error).
    Three windows on the S3 wire, at GET_CONFIG's S3 baud: (1) a burst of console output NaviCore prints on USB only
    (CONSOLE_BURST, an unknown '?HILQ<tag>' whose echo is a marker, GET_MESH_STATS), then QUIET_S more; (2) with
    navicore_reboot ticked too, a REBOOT inside nc_guard (s46 _restart; skipped while the recorder holds a take) from
    the command until QUIET_S after setup completes. In the burst the wire must get nothing at all: NaviCore drives the
    line idle the whole time. Across the restart GPIO8 floats from the reset until setup() begins Serial0 again
    (applySerialBauds(true), NaviCore.ino:4865, some 2 s in: :4688-4692), so what fails there is console text - a run
    of 4 or more printable characters
    (console_text) - and anything else read then is noted as line noise. (3) Every IDF line NaviCore printed on USB in
    those windows is counted and must not be on the wire. With none printed the IDF half of the finding was not
    exercised, and the note says so. Skips while a device or serialBcast out is on S3 (both write it)."""
    nc = _nc(bench)
    cfg = nc.config()
    _ports_free(cfg, "S3")
    _bcast_out_off(cfg, "S3")
    bauds, tag = nc_bauds(cfg), nonce()
    l = _wire(bench, "S3", bauds)
    problems, notes, idf, seen = [], [], [], b""
    try:
        m, nm = l.mark(), nc.dev.mark()
        for cmd in CONSOLE_BURST + (f"?HILQ{tag}",):
            nc.cli(cmd)
        nc.mesh_stats()
        nc.wcb_status()
        time.sleep(QUIET_S)
        idf += [x for x in nc.dev.since(nm) if IDF_LINE.match(x)]
        got, errs = l.received(m), l.errors(m)
        seen += got
        if got or errs:
            problems.append(f"during a console burst S3's wire got {_q(got)}"
                            + (f" and {len(errs)} RXERR" if errs else "")
                            + (" - NaviCore's own console" if f"HILQ{tag}".encode() in got else ""))
        why = nc.restart_blocker() if "navicore_reboot" in optin.enabled(bench.cfg) else "navicore_reboot is off"
        if why:
            notes.append(f"the restart window was not watched ({why})")
        else:
            with nc_guard(bench) as g:
                m, nm = l.mark(), g.nc.dev.mark()
                _restart(g.nc, "json")
                time.sleep(QUIET_S)
                idf += [x for x in g.nc.dev.since(nm) if IDF_LINE.match(x)]
                got, errs = l.received(m), l.errors(m)
            seen += got
            text = console_text(got)
            if text:
                problems.append(f"across a restart S3's wire got console text: {_q(b' / '.join(text))}")
            elif got or errs:
                notes.append(f"across the restart S3's wire read {len(got)} byte(s) of line noise and {len(errs)} RXERR, "
                             f"no text (GPIO8 floats until Serial0 begins)")
    finally:
        _release(l)
    leaked = [x for x in idf if x[x.index(")") + 1:].strip().encode()[:24] in seen]
    notes.append(f"{len(idf)} IDF log line(s) on NaviCore's USB in the windows"
                 + ("" if idf else " - the IDF half was not exercised (no hook makes one on demand)"))
    if leaked:
        problems.append(f"{len(leaked)} IDF log line(s) also went out S3")
    bench.note("s3_console_quiet: " + "; ".join(notes))
    assert not problems, (f"(should, nc.aux.uart0_console_leak) NaviCore's console reached S3's TX (UART0): "
                          f"{'; '.join(problems)}")


@test("ncwire.tx_interleave", "(should) A device write to S4 waits for the line the paced transmitter is clocking out "
      "there: #L21's HCR frames, and an HCR action, sent while a 95-character serial action goes out S4, each arrive "
      "whole on the S4 wire, before or after the line, never inside it (the map's nc.aux.tx_interleave; needs "
      "navicore_aux_tx too)", needs=["navicore"], links=["N20S4"], opt_in="navicore_wire")
def tx_interleave(bench):
    """The map's nc.aux.tx_interleave ('plausible'). A line to S4 - a serial action, or a mesh forward, which take the
    same path - goes out through auxTxPump a byte or so a loop() pass (NaviCore.ino:5216-5300; at 9600 one 1.04 ms byte
    a pass), so it is on the wire for its whole length while loop() runs everything else. A device write does not wait
    for it: #L21 prints hcrFormatCommand(2, 0, 80) and the raw frame straight to s4 (:3895-3908), an HCR action prints
    its payload to hcrDest's port (:1822), and a fade step, an MP3 Trigger, DFPlayer or WLED write likewise. Nothing
    arbitrates, so the device's bytes land inside the line: the device reads a garbled command, and whatever reads the
    line a broken one.
    The cue is the serial action's ACK: queueSerialAction has put the line in serialFwdQueue when NaviCore answers, and
    drainSerialFwd starts it in that same loop() pass (:2159-2164, :5279-5300), so a device write sent on the ACK lands a
    few bytes into a line that lasts ~100 ms at 9600. The wire itself cannot be the cue: the probe reports a burst only
    after 4 or more character times of quiet (probe_main.cpp bindChannel gapUs), and a paced line has none until it
    ends. Two cases, a 95-character serial action each: (1) #L21 on NaviCore's USB (no config change); (2) inside
    nc_guard, hcrDest local on S4, an HCR SetEmotion(H, 50) action (<OH50,QEH>, s44 HCR_CASES). Pass: the wire is the
    line and the device bytes one after the other, either order, each whole. Device-protocol bytes out NaviCore's own S4
    (J5): navicore_aux_tx must be ticked too (checked here, as s40 _reboot_opted does for restarts)."""
    _aux_tx_opted(bench)
    nc = _nc(bench)
    cfg = nc.config()
    _ports_free(cfg, "S4")
    _bcast_out_off(cfg, "S4")
    bauds = nc_bauds(cfg)
    line_ms = (SERIAL_CMD_KEEP + 1) * 10 * 1000 / bauds["S4"]
    if line_ms < 40:
        raise Skip(f"S4 runs at {bauds['S4']} baud: a line is on the wire {line_ms:.0f} ms, too short to land a write "
                   f"inside it on cue")
    l = _wire(bench, "S4", bauds)
    problems = []

    def case(what, line, dev, fire, ncx):
        m = l.mark()
        ack = ncx.test_action({"type": "serial", "port": "S4", "cmd": line[:-1].decode()})
        if not ack.get("ok"):
            problems.append(f"{what}: the serial action was answered {ack}")
            return
        fire()
        got = _await_bytes(l, m, [line, dev], 3.0)
        why = interleave_problem(got, line, dev)
        if why:
            problems.append(f"{what}: {why}")
    try:
        case("#L21 during a serial action", padded("IL1", SERIAL_CMD_KEEP).encode() + b"\r", HCR_TEST,
             lambda: nc.cli("#L21", until=r"^\[HCR TEST\] sent"), nc)
        with nc_guard(bench) as g:
            g.nc.set_config({"hcrDest": {"transport": "serial", "port": "S4"}})
            time.sleep(SETTLE_S)
            case("an HCR action during a serial action", padded("IL2", SERIAL_CMD_KEEP).encode() + b"\r",
                 b"<OH50,QEH>\n", lambda: g.nc.test_action(hcr(2, 0, 50)), g.nc)
    finally:
        _release(l)
    assert not problems, (f"(should, nc.aux.tx_interleave) a device write to S4 cut into a paced line: "
                          f"{'; '.join(problems)}")


@test("ncwire.rx_monitor_bcast_in", "A line typed into NaviCore's S3, S4 or S5 shows on its console as '[DISPATCH] "
      "Serial RX [<label>]  <text>' (non-printables as '.'), a line cut by 60 ms of quiet as a fragment; with serialBcast "
      "in on S4 (guarded) a whole line is broadcast to the mesh - every WCB port that takes broadcasts gets it once - "
      "and goes out S5 (out on) but never back out S4; a fragment, or a line on a port without 'in', is not broadcast",
      needs=["navicore", "wcb1"],
      links=["N20S3", "N20S4", "N20S5", "W1S1|W1S2|W1S3|W1S4|W1S5|W2S2|W2S3|W2S4|W2S5"], opt_in="navicore_wire")
def rx_monitor_bcast_in(bench):
    """The map's nc.aux.rx_monitor, nc.aux.bcast_in and nc.tx.serial_bcast_in. pollAuxSerialRx drains each port every
    loop() pass (NaviCore.ino:3980-3988); auxRxPollPort ends a line at CR or LF, at 127 characters, or after 60 ms of
    quiet - those two as fragments - and shows a byte outside 32-126 as '.' (:3965-3979). auxRxLine prints '[DISPATCH]
    Serial RX [<label>]  <text>' under DBG_SERIAL and, for a whole line on a port with serialBcast in and no device,
    broadcasts it to the mesh unensured and queues it for every other port with serialBcast out (never the one it came
    in on), then prints '[DISPATCH] Serial RX [<label>] → mesh broadcast' (:3942-3958, queueSerialBroadcastOut
    :3154-3163). Every WCB writes a plain broadcast out each port its ?BCAST,OUT flags open (s21 _expected_ports): each
    such probe-wired port must get it once. The broadcast is unacknowledged, so a line that reached no WCB port at all is
    typed once more before it counts. serialBcast is saved only inside nc_guard; the test skips while any port already
    has a flag on (it sets its own) or a device is routed to S3-S5."""
    nc = _nc(bench)
    cfg = nc.config()
    _ports_free(cfg, "S3", "S4", "S5")
    flags = [f"{p} {k}" for p, f in (cfg.get("serialBcast") or {}).items() for k in ("in", "out") if (f or {}).get(k)]
    if flags:
        raise Skip(f"serialBcast is already on ({', '.join(flags)}): this test saves its own")
    expected = _bcast_ports(bench)
    if not expected:
        raise Skip("no probe-wired WCB port to watch a mesh broadcast on")
    bauds, tag = nc_bauds(cfg), nonce()
    wires = {p: _wire(bench, p, bauds) for p in ("S3", "S4", "S5")}
    labels = {p: aux_label(cfg, p) for p in wires}
    problems = []

    def typed(port, data, settle=None):
        """Type `data` into NaviCore's `port` from the probe -> NaviCore's console lines since."""
        nm = nc.dev.mark()
        wires[port].send(data)
        return _trace(nc, nm, TYPED_S if settle is None else settle)
    try:
        for l in wires.values():
            l.send(b"\r")                    # end any partial line an earlier test left in NaviCore's buffers
        time.sleep(0.2)
        with nc.debug(DBG_SERIAL):
            for port in wires:
                text = f"HILR{tag}{port}"
                if f"[DISPATCH] Serial RX [{labels[port]}]  {text}" not in typed(port, text.encode() + b"\r"):
                    problems.append(f"{port}: a line typed in did not show as '[DISPATCH] Serial RX "
                                    f"[{labels[port]}]  {text}'")
            lines = typed("S4", f"HILN{tag}".encode() + b"\x01\x7f" + b"X\r")
            if f"[DISPATCH] Serial RX [{labels['S4']}]  HILN{tag}..X" not in lines:
                problems.append("S4: bytes 0x01 and 0x7F did not show as '.'")
            lines = typed("S4", f"HILF{tag}".encode(), settle=FRAGMENT_S)
            if f"[DISPATCH] Serial RX [{labels['S4']}]  HILF{tag}" not in lines:
                problems.append("S4: a line left unterminated for 60 ms did not show as a fragment")
            wires["S4"].send(b"\r")
        with nc_guard(bench) as g:
            g.nc.set_config({"serialBcast": {"S4": {"in": True, "out": True}, "S5": {"out": True}}})
            time.sleep(SETTLE_S)
            with g.nc.debug(DBG_SERIAL):
                for attempt in (1, 2):
                    text = f"HILI{tag}{attempt}"
                    watch = Watch(wires["S4"], wires["S5"], *[pl for pl, _ in expected.values()])
                    nm = g.nc.dev.mark()
                    wires["S4"].send(text.encode() + b"\r")
                    _await_bytes(wires["S5"], watch.marks[wires["S5"].key], [text.encode() + b"\r"], 2.0)
                    time.sleep(MESH_WAIT_S)
                    lines = _trace(g.nc, nm, 0.1)
                    copies = _port_copies(expected, watch, text)
                    reached = any(watch.got(pl).count(text.encode() + b"\r") for pl, _ in expected.values())
                    if not reached and attempt == 1:
                        bench.note(f"rx_monitor_bcast_in: no WCB port got {text} (an unacknowledged broadcast lost?) - "
                                   f"typing it once more")
                        continue
                    break
                if f"[DISPATCH] Serial RX [{labels['S4']}] → mesh broadcast" not in lines:
                    problems.append("S4 with serialBcast in: no '→ mesh broadcast' line")
                problems += [f"a line in on S4, {x}" for x in copies]
                if watch.got(wires["S5"]) != text.encode() + b"\r":
                    problems.append(f"S5 (serialBcast out) got {_q(watch.got(wires['S5']))}, expected the line once")
                if text.encode() in watch.got(wires["S4"]):
                    problems.append("the line went back out S4, the port it came in on")
                for port, data, what in (("S4", f"HILG{tag}".encode(), "a fragment on S4"),
                                         ("S3", f"HILJ{tag}".encode() + b"\r", "a line on S3, which has no 'in'")):
                    text = data.rstrip(b"\r").decode()
                    watch = Watch(*wires.values(), *[pl for pl, _ in expected.values()])
                    lines = typed(port, data, settle=max(FRAGMENT_S, MESH_WAIT_S))
                    if port == "S4":
                        wires["S4"].send(b"\r")
                    if any("→ mesh broadcast" in x for x in lines):
                        problems.append(f"{what} was broadcast")
                    hit = [k for k, (pl, _) in expected.items() if text.encode() in watch.got(pl)]
                    hit += [p for p, pl in wires.items() if text.encode() in watch.got(pl)]
                    if hit:
                        problems.append(f"{what} reached {', '.join(hit)}")
    finally:
        _release(*wires.values())
    assert not problems, "; ".join(problems)


# ============================================================ SBUS OUT and the Maestro bus
@test("ncwire.sbus_out_tee", "NaviCore's SBUS OUT is its SBUS input teed byte for byte: at 100000 8E2 inverted the wire "
      "carries back-to-back copies of the frame #L13 shows, which decodes to #L09's channels, at #L09's frame rate "
      "(about 9 ms apart on the probe's clock) with no framing or parity error; with sbusOutEnabled off it is silent",
      needs=["navicore"], links=["N20SBO"], opt_in="navicore_wire")
def sbus_out_tee(bench):
    """The map's nc.sbusout.tee. SbusReader::read writes every byte it takes off SBUS IN to the passthrough sink before
    parsing it (sbus_reader.h:76-93), and on the v2 profile that sink is UART1's own TX, 100k 8E2 inverted on GPIO5
    (applySbusOut, NaviCore.ino:3477-3498; :4836-4846). So SBUS OUT is the controller's stream as NaviCore received it:
    with the controller at rest every frame is the same bytes, and #L13 (the last frame parsed, :3856-3883) is that
    frame. The probe's hardware channel listens SBUS_WINDOW_S at 100000 8E2 inverted (the N20SBO wire's own format). The
    wire must be whole copies of #L13's frame back to back (frame_walk: a partial frame only at each edge of the window),
    at least 70 % of what #L09's fps says the window holds, a frame period within 1 ms (or 10 %) of 1000 / fps on the
    probe's clock (frame_times), no RXERR, and the frame must decode (hil/sbus.py decode, INF2) to the channels #L09
    prints. Skips unless SBUS is in at full rate. sbusOutEnabled off (not this bench) drops the sink: the wire must then
    stay silent, which is all that is checked. Enabling and disabling the tee live is nccfg.sbus_out_toggle's (log
    lines); this test changes nothing."""
    nc = _nc(bench)
    cfg = nc.config()
    l = bench.links.nc_require("SBO")
    try:
        if not cfg.get("sbusOutEnabled"):
            l.listen(hw=True)
            m = l.mark()
            time.sleep(QUIET_S)
            got, errs = l.received(m), l.errors(m)
            assert not got and not errs, (f"sbusOutEnabled is off, yet SBUS OUT carried {len(got)} bytes "
                                          f"({len(errs)} RXERR) in {QUIET_S:g} s")
            bench.note("sbus_out_tee: sbusOutEnabled is off, and SBUS OUT was silent")
            return
        st = nc.sbus_full_rate()
        if st["fps"] < SBUS_FULL_FPS:
            raise Skip(f"NaviCore reads SBUS at {st['fps']} fps: no full-rate stream to tee")
        l.listen(hw=True)
        m = l.mark()
        time.sleep(SBUS_WINDOW_S / 2)
        frame = nc.sbus_raw()
        dump = nc.sbus_dump()
        time.sleep(SBUS_WINDOW_S / 2)
        data, bursts, errs = l.received(m), l.bursts(m), l.errors(m)
    finally:
        _release(l)
    if not frame:
        raise Skip("#L13: NaviCore has parsed no SBUS frame")
    problems = []
    start, count, why = frame_walk(data, frame)
    if why:
        problems.append(why)
    fps = dump["fps"] or st["fps"]
    want = SBUS_WINDOW_S * fps * 0.7
    if count < want:
        problems.append(f"{count} whole frames in {SBUS_WINDOW_S:g} s, under 70 % of the {SBUS_WINDOW_S * fps:.0f} "
                        f"#L09's {fps} fps makes")
    period = None
    if count >= 3:
        t = frame_times(bursts, start, len(frame), count)
        period = (t[-1] - t[0]) / (count - 1)
        if abs(period - 1000 / fps) > max(1.0, 0.1 * 1000 / fps):
            problems.append(f"frames {period:.2f} ms apart on the probe's clock; #L09's {fps} fps is "
                            f"{1000 / fps:.2f} ms")
    if errs:
        kinds = sorted({e.split()[3] for e in errs if len(e.split()) > 3})
        problems.append(f"{len(errs)} RXERR at 100000 8E2 inverted ({', '.join(kinds)})")
    chans = sbus_decode(frame)["channels"]
    if dump["channels"] and chans[:len(dump["channels"])] != dump["channels"]:
        problems.append(f"#L13's frame decodes to {chans}, #L09 prints {dump['channels']}")
    bench.note(f"sbus_out_tee: {count} copies of the {len(frame)}-byte frame in {SBUS_WINDOW_S:g} s"
               + (f", {period:.2f} ms apart" if period else "") + f"; #L09 {fps} fps")
    assert not problems, "SBUS OUT: " + "; ".join(problems)


@test("ncwire.maestro_bus_tap", "NaviCore's Maestro bus carries each ?MAE query on a local slot as its exact Pololu frame "
      "at GET_CONFIG's Maestro baud - getPosition AA <dev> 10 <ch>, getMovingState AA <dev> 13, getErrors AA <dev> 21 - "
      "and nothing else, and the Maestro answers each through the tap; on a hook image the wire equals NaviCore's "
      "DBG_WIRE Serial2 copy (reads only: nothing moves)", needs=["navicore"], links=["N20MAE"], opt_in="navicore_wire")
def maestro_bus_tap(bench):
    """The local Maestro bus as bytes (the map's nc.mae.serial2 and nc.mae.frames; the bus half NC-WP7 could only read
    through DBG_WIRE). ?MAE,GET/MOVING/ERR on a type-1 slot (NaviCore.ino:3589-3631) drain Serial2's RX, write one frame
    through maestroWrite - 0xAA, the slot's Pololu device, the command with its MSB cleared, then a channel for
    getPosition (:692-735) - and read the 2- or 1-byte reply within 25 ms (maestroLocalQuery :770-787), printing
    [MAE:<slot>]{...,"val":n} on an answer and "err":"timeout" without one. The tap listens on NaviCore's TX line at
    auxBaud.maestro (57600 here, so a hardware channel), alongside the Maestro: each query must put its frame there
    exactly, once, and the Maestro must still answer (the tap does not load the line). getErrors clears the Maestro's
    error register, as ?MAE,ERR always does. The test waits SETTLE_S first: every config save re-applies easing on the
    bus and repeats it twice 500 ms apart (s44), and the previous test may have saved."""
    nc = _nc(bench)
    cfg = nc.config()
    slots = NaviCore.local_slots(cfg)
    if not slots:
        raise Skip("NaviCore has no local (type 1) Maestro slot: nothing writes its Maestro bus")
    slot, dev = slots[0][0], int(slots[0][1])
    bauds, hook = nc_bauds(cfg), _hooks(nc)
    time.sleep(SETTLE_S)
    l = _wire(bench, "MAE", bauds)
    problems, answers = [], []
    try:
        m0 = l.mark()
        with nc.debug(DBG_WIRE if hook else 0):
            for q, verb, ch in (("pos", "GET", 0), ("pos", "GET", 5), ("mov", "MOVING", None), ("err", "ERR", None)):
                cmd = f"?MAE,{verb},{slot}" + (f",{ch}" if ch is not None else "")
                want = query_frame(q, dev, ch or 0)
                m, nm = l.mark(), nc.dev.mark()
                nc.cli(cmd)
                got = _await_bytes(l, m, [want], 1.0)
                time.sleep(0.2)
                got = l.received(m)
                lines = _trace(nc, nm, 0.05)
                val = parse_mae("\n".join(lines), slot, q, ch)
                answers.append(f"{cmd}: {val}")
                if got != want:
                    problems.append(f"{cmd}: the Maestro bus got {got.hex(' ') or 'nothing'}, expected {want.hex(' ')} "
                                    f"at {bauds['MAE']} baud")
                if not isinstance(val, int):
                    problems.append(f"{cmd}: the Maestro did not answer ({val})")
                if hook:
                    logged, gaps = wire_log(lines, "Serial2")
                    if not gaps and logged != got:
                        problems.append(f"{cmd}: NaviCore's [WIRE] Serial2 log says {logged.hex(' ') or 'nothing'}, "
                                        f"the wire got {got.hex(' ') or 'nothing'}")
        errs = l.errors(m0)
    finally:
        _release(l)
    if errs:
        problems.append(f"{len(errs)} RXERR on the Maestro bus at {bauds['MAE']} baud")
    bench.note(f"maestro_bus_tap: slot {slot} (device {dev}) at {bauds['MAE']} baud: {'; '.join(answers)}"
               + ("" if hook else "; not a hook image: the DBG_WIRE copy was not compared"))
    assert not problems, "; ".join(problems)
