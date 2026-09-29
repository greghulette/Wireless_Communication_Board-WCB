"""NaviCore's device transports and its Maestro: the remote Maestro stream and every Maestro verb as bytes, the
remote Maestro read, NaviCore's own Maestro 1 read back, the HCR, MP3 Trigger, DFPlayer and WLED through a WCB and on
NaviCore's own ports, serial actions and the mesh-to-serial bridge (docs/hil_plan/NAVICORE.md NC-WP7, ids ncdev.*).

Line numbers are the NaviCore tree the bench image is built from (branch hil-week, 6925773, NAVICORE.md INF9), the
WcbCmd and WCB_Client copies in the Arduino-Code sketchbook that image compiles (WcbCmd 0.9.1), and this repo's
Code/WCB for the WCB side.

Where the bytes are seen:
- The remote Maestro stream. On this bench slots 3-8 are remote (type 2) with devices 3-8 that nothing hosts
  (NAVICORE.md §1.2; s41 _remote_slot, slot 4 first). maestroWrite (NaviCore.ino:647-701) writes such a slot's Pololu
  frame into the broadcast WCBStream; both WCBs are Maestro_Remote (?MAESTRO,REMOTE) and write each Kyber chunk to
  their S1: the W1S1 probe and the listen-only tap on W2 S1 read it byte-exact, and real Maestro 2 ignores device 4.
  The broadcast is unacknowledged, so a frame NaviCore wrote (its hook image's [WIRE] WCBStream copy) that a wire
  never got is a lost packet: the Maestro steps try once more before it counts.
- Devices through a WCB. NaviCore's HCR / MP3 Trigger / DFPlayer / WLED actions become ;H / ;A / ;D / ;L commands to
  W2 (executeHcrAction :1600-1650, executeMp3Action :1776-1835, executeDfpAction :1901-1958, executeWledAction
  :1960-2020), which formats them with the same WcbCmd translators and writes its probe-wired S4 (an HCR, s15
  _hcr_w2), S5 (an MP3 Trigger or DFPlayer, s15 _w2_audio) or S2 (WLED 1, as bench config has it). The expected bytes
  are the ones s15 and s09 assert for the same verbs typed on W1 (the WcbCmd golden vectors), and NaviCore's own
  dispatch trace names the command it sent: a NaviCore regression (a wrong verb) and a WcbCmd one (a wrong byte for
  the right verb) are told apart.
- NaviCore's own ports. No probe is wired to S3-S5, SBUS OUT or the Maestro bus (D-NC37), so their bytes are the
  NAVICORE_HIL_HOOKS image's DBG_WIRE copy (navicore_hil.h wire(): '[WIRE] <port> <offset>/<length>: <hex>', one line
  per 48 bytes of each block handed to S3, S4, S5, Serial2 or the WCBStream). vlogf drops a whole line when the USB TX
  ring is short, so wire_log() reports a block whose lines do not cover it instead of reading the hole as a wrong
  byte. A test that needs those bytes skips on any other image. The local tables are the remote ones: the same
  action must give the same bytes on both transports (WCB CLAUDE.md rule 1, the WcbCmd dual compile), checked end to
  end here for the first time.
- NaviCore's own Maestro 1 (the dome) reads back through ?MAE,GET / MOVING / ERR (NaviCore.ino:3398-3450).

What moves, what is written, what is put back (each test's docstring has the detail):
- Nothing moves in the remote-Maestro and device tests; W2's device configs are set and cleared inside config_guard
  (s15's fixtures), NaviCore's inside nc_guard (D-NC2). getErrors reads, and so clears, Maestro 2's error register.
- The local-Maestro tests (hil/servos.py) move one channel of NaviCore's Maestro 1: the lowest one no passthrough knob
  drives, the one navicore.maestro_mesh_settarget_readback already moves on every run (ch 1 here, off at rest: every
  undriven dome channel read 0 in run 20260928-204259). They drive it between 5800 and 6400 quarter-us and leave it
  as found (off, or its old position), with speed and accel 0.
- Opt-in navicore_aux_tx gates everything that routes an HCR, MP3 Trigger, DFPlayer or WLED to NaviCore's own S3-S5
  and #L20/#L21: device-protocol bytes go out J4-J6, where nothing is recorded as attached. Plain serial text (the
  serial action, the mesh-to-serial bridge) is not gated, as navicore.serial_route_dbg already sends it on every run;
  those tests skip while a device is routed to the port.
- A test that stalls loop() long enough to overflow the SBUS UART (#L90, or a long line to a bit-banged port) makes
  the engine inert first (s41 _engine_inert, inside nc_guard) and is in hil/servos.py: SBUS OUT pauses meanwhile.
"""
import json
import random
import re
import statistics
import time

from hil import optin
from hil.nc_guard import nc_guard
from hil.navicore import DBG_DFP, DBG_HCR, DBG_MAESTRO, DBG_MP3, DBG_SERIAL, DBG_WLED, NaviCore, parse_mae
from hil.runner import Skip, test
from suites.common import Console, config_guard, link, nonce, snapshot, usb_wcb
from suites.s09_wled import CASES as WLED_VERBS
from suites.s15_hcr_mp3_dfp import STOP_BURST, _dfp, _hcr_w2, _lf, _vol3, _w2_audio
from suites.s21_navicore_sbus import _sbus_setup
from suites.s22_maestro_kyber import _settle_maestro2
from suites.s40_navicore_config import HOOK_SKIP, _all_actions, _flushed, _hooks
from suites.s41_navicore_engine import _engine_inert, _hosted_remote_slot, _probe_ms, _remote_slot
from suites.s42_navicore_sbus_engine import (CMD_ACCEL, CMD_SPEED, CMD_TARGET, DBG_WIRE, Sticks, _free_channels,
                                             _free_knobs, _knob, _out, _stick, axis_value, knob_pos, pololu_frames)

CMD_GO_HOME, CMD_STOP_SCRIPT, CMD_SUB, CMD_SUB_P = 0x22, 0x24, 0x27, 0x28   # WcbCmd WcbMaestro.h:66-69
WCBSTREAM_CAP = 177              # WCBStream _buf[177] (WCB_Client WCBStream.h:104): what one Kyber packet can carry
MAE_CMD_KEEP = 35                # executeMaestroCmd copies an action's cmd into char buf[36] (NaviCore.ino:1308)
SERIAL_CMD_KEEP = 95             # RcAction.cmd[96] (rc_config.h:130): what actionFromJson keeps of a cmd
FWD_QUEUE = 4                    # serialFwdQueue = xQueueCreate(4, ...) (NaviCore.ino:4845)
FWD_STALL_MS = 3000              # the #L90 stall mesh_forward_burst uses
SBUS_BUFFER_MS = 96              # Serial1's 256-byte RX ring + the 128-byte FIFO at SBUS-24's 4 bytes/ms (s41)
PACED_MAX_S = 0.050              # serial_action_paced: what a paced write may add to TEST_ACTION's round trip


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


# ------------------------------------------------------------------ pure helpers (selftest.py feeds them fixtures)
WIRE_LINE = re.compile(r"^\[WIRE\] (\S+) (\d+)/(\d+): ([0-9A-F]{2}(?: [0-9A-F]{2})*)$")
KYBER_LINE = re.compile(r"^\[WCBStream\] Broadcast \(Kyber\) (\d+) bytes — (OK|FAIL)$")


def wire_log(lines, port):
    """The bytes a NAVICORE_HIL_HOOKS image logged under DBG_WIRE for `port` ('S3', 'S4', 'S5', 'Serial2' or
    'WCBStream'), joined in order -> (bytes, gaps). wire() prints one '[WIRE] <port> <offset>/<length>: <hex>' line per
    48 bytes of each block a port accepted (navicore_hil.h), through vlogf, which drops a whole line rather than block
    when the USB TX ring is short. `gaps` names every block whose lines do not cover it from 0 to its length, so a lost
    log line is reported as that, never read as a wrong byte."""
    data, gaps = bytearray(), []
    total, nxt = None, 0
    for x in lines:
        m = WIRE_LINE.match(x.rstrip())
        if not m or m.group(1) != port:
            continue
        off, n, chunk = int(m.group(2)), int(m.group(3)), bytes.fromhex(m.group(4).replace(" ", ""))
        if total is not None and (off != nxt or n != total):
            gaps.append(f"{port}: a {total}-byte block stopped at byte {nxt}")
            total = None
        if total is None:
            if off:
                gaps.append(f"{port}: bytes 0-{off - 1} of a {n}-byte block were never logged")
            total = n
        data += chunk
        nxt = off + len(chunk)
        if nxt >= total:
            total, nxt = None, 0
    if total is not None:
        gaps.append(f"{port}: a {total}-byte block stopped at byte {nxt}")
    return bytes(data), gaps


def kyber_packets(lines):
    """[(bytes, 'OK'|'FAIL')] of the WCBStream's flushes: '[WCBStream] Broadcast (Kyber) <n> bytes — OK', printed for
    every packet NaviCore's broadcast stream sends (WCB_Client WCBStream.cpp _flushBuffer, wcbStreamLog: ungated, and
    dropped only when USB has no room for it)."""
    return [(int(m.group(1)), m.group(2)) for x in lines for m in [KYBER_LINE.match(x.rstrip())] if m]


def stream_packets(lengths, cap=WCBSTREAM_CAP):
    """The packet sizes a burst of Pololu frames of these lengths, written in one loop() pass, leaves NaviCore in:
    maestroWrite flushes what is buffered before a frame that would not fit whole (NaviCore.ino:668-670 for a
    subroutine frame, :679-682 for the others), and the rest goes at the next WCBStream tick."""
    out, cur = [], 0
    for n in lengths:
        if cap - cur < n:
            out.append(cur)
            cur = 0
        cur += n
    if cur:
        out.append(cur)
    return out


def pololu(dev, cmd, a=None, value=None):
    """One Pololu-protocol frame as NaviCore writes it: AA <device> <cmd & 0x7F>, then a channel or subroutine byte as
    given (NOT masked: maestroRestartScript and maestroSubParam write it as is, NAVICORE.md D-NC23), then a 14-bit
    value as two 7-bit bytes, low first (maestroSetTarget/SetSpeed/SetAccel/SubParam, NaviCore.ino:1033-1101)."""
    out = bytes([0xAA, dev & 0x7F, cmd & 0x7F])
    if a is not None:
        out += bytes([a & 0xFF])
    if value is not None:
        out += bytes([value & 0x7F, (value >> 7) & 0x7F])
    return out


def frame_lengths(data):
    """The length of each Pololu frame in `data` (pololu_frames' table), for stream_packets."""
    table = {CMD_TARGET: 6, CMD_SPEED: 6, CMD_ACCEL: 6, CMD_SUB_P: 6, CMD_SUB: 4, CMD_GO_HOME: 3, CMD_STOP_SCRIPT: 3}
    out, i = [], 0
    while i + 2 < len(data):
        n = table.get(data[i + 2]) if data[i] == 0xAA else None
        if n is None:
            raise ValueError(f"no Pololu frame at byte {i} of {data.hex(' ')}")
        out.append(n)
        i += n
    return out


def dev_stream(data, dev):
    """The bytes of every frame for Maestro `dev` in `data`, re-joined: what a wire received for that device, with other
    devices' frames (a knob on another remote slot) left out."""
    out = b""
    for d, cmd, ch, val in pololu_frames(data):
        if d == dev:
            out += pololu(dev, cmd, ch, val)
    return out


def hcr_payload(lines, port, fn, chan, track):
    """The HCR bytes NaviCore's local HCR dispatch line shows for (fn, chan, track) on `port`, or None when there is
    none. executeHcrAction prints '[DISPATCH] HCR→<port>  fn=<fn> chan=<chan> track=<track>  <payload>' with the payload
    as it goes out, frames and newlines included (NaviCore.ino:1722-1724), so the console shows the first frame on that
    line and any further ones on their own lines after it."""
    head = f"[DISPATCH] HCR→{port}  fn={fn} chan={chan} track={track}  "
    for i, x in enumerate(lines):
        x = x.rstrip()
        if x.startswith(head):
            frames = [x[len(head):]]
            for y in lines[i + 1:]:
                y = y.rstrip()
                if not re.fullmatch(r"<[^<>]*>", y):
                    break
                frames.append(y)
            return "".join(f + "\n" for f in frames)
    return None


def aux_label(cfg, port):
    """auxPortLabel (NaviCore.ino:274-279): the NaviCore v2 profile silkscreens S3-S5 as 'Serial 1-3', WCB HW 3.2 as
    'Serial 3-5'."""
    i = {"S3": 0, "S4": 1, "S5": 2}[port]
    return f"Serial {i + 1 if cfg.get('boardType', 0) == 0 else i + 3}"


def free_profiles(cfg):
    """Every smoothing-profile index 1-5 with no entries that no knob uses and no action names (s40 _free_profile's
    test, all of them): changing one changes no easing anything runs."""
    profs = cfg.get("smoothProfiles") or []
    used = {k.get("smoothProfile") for k in (cfg.get("knobs") or {}).values()}
    cmds = [str(a.get("cmd", "")).lower() for _, a in _all_actions(cfg)]
    return [p for p in range(1, len(profs)) if not profs[p].get("entries") and p not in used
            and not any(re.search(rf",p{p}(?!\d)", c) for c in cmds)]


def profile_without(cfg, slot):
    """A smoothing-profile index whose entries name no channel of Maestro `slot`, or None: restartScript,<n>,p<it> then
    loads nothing on that slot, whatever the slot's switch easing is (the action's own easing wins, NaviCore.ino:1368-
    1384), so the frame it writes is the subroutine frame alone."""
    for p, prof in enumerate(cfg.get("smoothProfiles") or []):
        if not any(e.get("mid") == slot for e in prof.get("entries") or []):
            return p
    return None


def managed_channels(cfg, slot):
    """Channels of Maestro `slot` that some smoothing profile gives a speed or accel: the ones
    maestroResetSmoothedChannels zeroes (NaviCore.ino:1106-1116)."""
    return sorted({e.get("ch") for p in cfg.get("smoothProfiles") or [] for e in p.get("entries") or []
                   if e.get("mid") == slot and (e.get("spd") or e.get("acc"))})


# ------------------------------------------------------------------ the action tables
def hcr(fn, chan=0, track=0):
    return {"type": "hcr", "fn": fn, "chan": chan, "track": track}


# (fn, chan, track, the ;H command NaviCore sends a WCB for it (hcrFormatWcbCommand, NaviCore.ino:1499-1555), the HCR
# bytes). The bytes are what WcbCmd's HcrCodec formats (WcbHcr.cpp:55-122) and what W2 writes for that ;H command
# (s15's hcr.verbs_*, hcr.fn_codec). Stateful where a volume steps: the first row sets V, A and B to 40.
HCR_CASES = (
    (17, 3, 40, ";H,VOL,40", _vol3(40)),
    (2, 0, 50, ";H,SETEMOTION,H,50", _lf("<OH50,QEH>")),
    (3, 1, 1, ";H,TRIGGER,S,STRONG", _lf("<SS1,QES,QT>")),
    (4, 2, 0, ";H,STIM,M,MOD", _lf("<SM0,QEM,QT>")),
    (5, 0, 0, ";H,OVERLOAD", _lf("<SE,QT>")),
    (6, 0, 0, ";H,MUSE", _lf("<MM>")),
    (7, 5, 30, ";H,MUSE,GAP,5,30", _lf("<MN5,MX30>")),
    (8, 0, 0, ";H,STOP", STOP_BURST),
    (9, 0, 0, ";H,STOPEMOTE", _lf("<PSV,QT>")),
    (10, 1, 0, ";H,OVERRIDE,1", _lf("<O1,QO>")),
    (11, 0, 0, ";H,RESETEMOTIONS", _lf("<OR,QE>")),
    (13, 0, 1, ";H,MUSE,1", _lf("<M1,QM>")),
    (14, 1, 5, ";H,PLAY,A,5", _lf("<CA0005,QPA>")),
    (14, 0, 7, ";H,FN,14,0,7", _lf("<CV0007,QPV>")),        # V has no PLAY verb: the numeric form (:1534-1535)
    (16, 2, 0, ";H,STOPWAV,B", _lf("<PSB,QPB>")),
    (16, 0, 0, ";H,FN,16,0,0", _lf("<PSV,QPV>")),           # ...nor STOPWAV (:1536-1537)
    (17, 1, 60, ";H,VOL,A,60", _lf("<PVA60>")),
    (18, 0, 0, ";H,VOLUP", _lf("<PVV45>", "<PVA65>", "<PVB45>")),   # fn 18/19 chan 0 = all, step 0 = 5
    (18, 2, 10, ";H,VOLUP,A,10", _lf("<PVA75>")),           # ...chan 1/2/3 = V/A/B (:1544-1550)
    (19, 3, 0, ";H,VOLDN,B", _lf("<PVB40>")),
    (12, 1, 0, ";H,FADEIN,A,0", _lf("<PVA75>")),            # an instant fade: one SetVolume to the level
    (15, 2, 0, ";H,FADEOUT,B,0", _lf("<PVB0>", "<PSB,QPB>", "<PVB40>")),
    (20, 0, 0, ";H,FN,20,0,0", _lf("<PSG>")),               # no verb for 20/21: the default branch (:1551)
    (21, 0, 0, ";H,FN,21,0,0", _lf("<PSG>", "<PSA,QPA>", "<PSB,QPB>")),
)
# Refused before anything is sent, with the line each prints ('WCB' or 'Serial' for the transport): emotion 4 (no
# Overload shortcut since WcbCmd 0.9: HcrCodec::normalize, WcbHcr.cpp:11-15), a fade on V (A/B only, :1628-1631 and
# :1669-1672), an unknown fn, SetEmotion 100 (0-99), SetVolume 101 (0-100).
HCR_REFUSED = ((4, 4, 0, "bad"), (12, 0, 2, "fade"), (1, 0, 0, "bad"), (2, 0, 100, "bad"), (17, 0, 101, "bad"))


def hcr_refusal(transport, fn, chan, track, kind):
    if kind == "fade":
        return f"[DISPATCH] HCR-{transport}: fade chan must be A(1)/B(2), got {chan} — skipped"
    return f"[DISPATCH] HCR-{transport}: bad/unsupported fn={fn} chan={chan} track={track} — skipped"


# (fn, track, the ;A command, the MP3 Trigger bytes, the volume shadow after it) - mp3FormatCommand (NaviCore.ino:
# 1734-1760) and Mp3Codec::handle (WcbCmd WcbMp3.cpp:32-95): 'v' <volume> before every play, VOLUP/VOLDN step 5.
MP3_CASES = (
    (6, 25, ";A,VOL,25", "7619", 25),
    (1, 5, ";A,PLAY,5", "76197405", 25),
    (2, 3, ";A,PLAYFS,3", "76197003", 25),
    (3, 0, ";A,STOP", "4F", 25),
    (4, 0, ";A,NEXT", "46", 25),
    (5, 0, ";A,PREV", "52", 25),
    (7, 0, ";A,VOLUP", "7614", 20),
    (8, 0, ";A,VOLDN", "7619", 25),
    (1, 255, ";A,PLAY,255", "761974FF", 25),
    (2, 0, ";A,PLAYFS,0", "76197000", 25),
)
MP3_REFUSED = ((1, 0), (1, 256), (2, 256), (6, 65), (6, -1), (9, 0), (0, 0))

DFP_DEVICE_2 = bytes.fromhex("7E FF 06 09 00 00 02 FE F0 EF")   # the ;D,DEVICE,2 regression (WCB tracker #21)
# (fn, chan, track, the ;D command, the DFPlayer frame) - dfpFormatCommand (NaviCore.ino:1838-1880) and
# DfPlayerCodec::handle (WcbCmd WcbDfPlayer.cpp:52-160): 10-byte frames, VOLUP/VOLDN absolute in steps of 2.
DFP_CASES = (
    (9, 0, 25, ";D,VOL,25", _dfp(0x06, 25)),
    (1, 0, 5, ";D,PLAY,5", _dfp(0x03, 5)),
    (2, 1, 2, ";D,FOLDER,1,2", _dfp(0x0F, 0x0102)),
    (3, 0, 3, ";D,MP3FOLDER,3", _dfp(0x12, 3)),
    (4, 0, 0, ";D,STOP", _dfp(0x16)),
    (5, 0, 0, ";D,NEXT", _dfp(0x01)),
    (6, 0, 0, ";D,PREV", _dfp(0x02)),
    (7, 0, 0, ";D,PAUSE", _dfp(0x0E)),
    (8, 0, 0, ";D,RESUME", _dfp(0x0D)),
    (10, 0, 0, ";D,VOLUP", _dfp(0x06, 27)),
    (11, 0, 0, ";D,VOLDN", _dfp(0x06, 25)),
    (12, 0, 7, ";D,LOOP,7", _dfp(0x08, 7)),
    (13, 1, 0, ";D,LOOPALL,1", _dfp(0x11, 1)),
    (14, 2, 0, ";D,LOOPFOLDER,2", _dfp(0x17, 2)),
    (15, 0, 0, ";D,RANDOM", _dfp(0x18)),
    (16, 2, 0, ";D,EQ,2", _dfp(0x07, 2)),
    (17, 2, 0, ";D,DEVICE,2", DFP_DEVICE_2),
    (18, 0, 0, ";D,RESET", _dfp(0x0C)),
)
DFP_REFUSED = ((1, 0, 0), (1, 0, 3000), (2, 0, 1), (2, 1, 256), (9, 0, 31), (16, 6, 0), (17, 6, 0), (17, 0, 0),
               (19, 0, 0))
# The volume shadow after each DFP_CASES row.
DFP_VOLS = (25, 25, 25, 25, 25, 25, 25, 25, 25, 27, 25, 25, 25, 25, 25, 25, 25, 25)


def mae_cases(dev, prof):
    """(action cmd, the frames it must put on the wire) for Maestro verbs on a remote slot of device `dev`: every verb
    executeMaestroCmd parses (NaviCore.ino:1307-1390) and what each clamps or refuses. `prof` is a smoothing profile
    with no entries for that slot (profile_without), so a restartScript loads no easing before its subroutine frame."""
    def P(cmd, a=None, v=None):
        return pololu(dev, cmd, a, v)
    return (
        ("setTarget,5,6000", P(CMD_TARGET, 5, 6000)),
        ("setTarget,5,16383", P(CMD_TARGET, 5, 16383)),
        ("setTarget,5,20000", P(CMD_TARGET, 5, 16383)),      # clamped, not wrapped (maestroSetTarget :1035)
        ("setTarget,5,0", P(CMD_TARGET, 5, 0)),              # 0 = stop pulsing (the auto-release write)
        ("setTarget,31,6000", P(CMD_TARGET, 31, 6000)),
        ("setTarget,32,6000", b""),                          # refused: maestroChanOk (:937-942)
        ("setSpeed,5,10", P(CMD_SPEED, 5, 10)),
        ("setSpeed,5,20000", P(CMD_SPEED, 5, 16383)),        # saturated (:1062)
        ("setSpeed,32,10", b""),
        ("setAccel,5,200", P(CMD_ACCEL, 5, 200)),            # two 7-bit bytes, 48 01 (:1071-1082)
        ("setAccel,5,255", P(CMD_ACCEL, 5, 255)),
        ("setAccel,32,10", b""),
        ("setSpeedAccel,5,10,20", P(CMD_SPEED, 5, 10) + P(CMD_ACCEL, 5, 20)),
        ("goHome", P(CMD_GO_HOME)),
        ("stopScript", P(CMD_STOP_SCRIPT)),
        (f"restartScript,3,p{prof}", P(CMD_SUB, 3)),
        ("subParam,3,1000", P(CMD_SUB_P, 3, 1000)),
        ("subParam,3,20000", P(CMD_SUB_P, 3, 16383)),        # clamped (:1094)
        ("setTarget,5," + "0" * 20 + "6000", P(CMD_TARGET, 5, 600)),   # 36 characters: the parse keeps 35 (:1308)
        ("setTarget,5", b""),                                # no position: nothing (:1316)
        ("HILnoverb", b""),
    )


# ------------------------------------------------------------------ small drivers
def _ports_free(cfg, *ports):
    """Skip while NaviCore routes a device (HCR, MP3 Trigger, DFPlayer, WLED) to one of its own `ports`: this test's
    bytes would reach it, and a device's framing would be broken by them (NaviCore.local_devices)."""
    busy = [d for d in NaviCore.local_devices(cfg) if d.rsplit(" on ", 1)[-1] in ports]
    if busy:
        raise Skip(f"NaviCore routes {', '.join(busy)} to its own {'/'.join(ports)}: this test's bytes would reach it")


def _opted(bench, key):
    return key in optin.enabled(bench.cfg)


def _trace(nc, since, settle=0.3):
    """NaviCore's console lines since mark `since`, after a #L12 that releases a line it holds back (s40 _flushed)."""
    return _flushed(nc, since, settle=settle)


def _dispatch_ok(lines, prefix):
    return [x for x in lines if x.startswith(prefix)]


def _wire_problems(where, lines, port, want):
    """[] when the DBG_WIRE copy for `port` in `lines` is exactly `want`, else one line naming what differs (a lost log
    line is said to be one)."""
    got, gaps = wire_log(lines, port)
    if gaps:
        return [f"{where}: NaviCore's [WIRE] log for {port} lost lines ({'; '.join(gaps)}), so its bytes cannot be read"]
    if got != want:
        return [f"{where}: NaviCore wrote {got!r} to {port}, expected {want!r}"]
    return []


def _await_mae(nc, since, slot, q, ch=None, timeout=4.0):
    """The value (or error word) of the first [MAE:<slot>] marker for query `q` after mark `since`, or None. A remote
    read answers asynchronously (maePumpRemoteEmits, NaviCore.ino:829-850), and a lone line can sit unsent until more
    output follows it, so a #L12 goes out every 0.4 s while it waits (HIL_TESTING.md §5)."""
    deadline = time.monotonic() + timeout
    while True:
        got = parse_mae("\n".join(nc.dev.since(since)), slot, q, ch)
        if got is not None or time.monotonic() >= deadline:
            return got
        nc.dev.send("#L12")
        time.sleep(0.4)


def _ack_latency(nc, action, timeout=3.0):
    """Seconds from sending TEST_ACTION `action` to the host stamping its ACK line. NaviCore answers a USB TEST_ACTION
    after the action has run (NaviCore.ino:4017-4027), so the time a blocking write held loop() shows here."""
    m = nc.dev.mark()
    t0 = time.monotonic()
    nc.dev.send(json.dumps({"type": "TEST_ACTION", "action": action}, separators=(",", ":")))
    nc.dev.expect(r'^\{"type":"ACK","of":"TEST_ACTION"', timeout=timeout, since=m)
    ts = next(ts for ts, x in list(nc.dev.lines[m:]) if x.startswith('{"type":"ACK","of":"TEST_ACTION"'))
    return ts - t0


def _sbus_frames(nc):
    """(#L09's frame counter, the host time it was read)."""
    st = nc.sbus_dump()
    return int(st["frames"] or 0), time.monotonic()


class Wires:
    """W1 S1 and the W2 S1 tap read together for one remote Maestro device. step(send, want, what) marks both, runs
    `send`, and checks that each wire then received exactly the frames `want` for that device (dev_stream: another
    device's frames, a knob on another remote slot, are left out). The Kyber broadcast is unacknowledged, so when the
    hook image's [WIRE] WCBStream copy shows NaviCore wrote exactly `want` and a wire still missed it, the step is sent
    once more (noted in `retries`) before it counts; without that copy, or when NaviCore wrote something else, the
    first result stands. `problems` collects what differed."""

    def __init__(self, nc, dev, wires, hook):
        self.nc, self.dev, self.wires, self.hook = nc, dev, [w for w in wires if w is not None], hook
        self.problems, self.retries = [], []

    def _wait(self, marks, want, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(want in dev_stream(w.received(m), self.dev) for w, m in zip(self.wires, marks)):
                return
            time.sleep(0.05)

    def step(self, send, want, what, quiet=0.4, timeout=2.0, retry=True):
        """-> NaviCore's console lines of the (last) attempt."""
        for attempt in (1, 2):
            marks, nm = [w.mark() for w in self.wires], self.nc.dev.mark()
            send()
            if want:
                self._wait(marks, want, timeout)
            time.sleep(quiet if want else max(quiet, 0.8))
            got = [dev_stream(w.received(m), self.dev) for w, m in zip(self.wires, marks)]
            lines = _trace(self.nc, nm, 0.05)
            if all(g == want for g in got):
                return lines
            wrote = dev_stream(wire_log(lines, "WCBStream")[0], self.dev) if self.hook else None
            if attempt == 1 and retry and wrote == want:
                self.retries.append(what)
                continue
            note = "" if wrote is None else f" (NaviCore's [WIRE] WCBStream copy: {wrote.hex(' ') or 'nothing'})"
            self.problems += [f"{what}: {w.key} got {g.hex(' ') or 'nothing'}, expected {want.hex(' ') or 'nothing'}"
                              f"{note}" for w, g in zip(self.wires, got) if g != want]
            return lines


def _maestro(slot, cmd):
    return {"type": "maestro", "target": str(slot), "cmd": cmd}


# ============================================================ the remote Maestro stream
@test("ncdev.mae_remote_stream_exact", "A remote Maestro slot's frames reach both Maestro_Remote WCBs byte-exact: "
      "?MAE,<slot>,5,6000 is AA <dev> 04 05 70 2E on W1 S1 and the W2 S1 tap, one 6-byte packet; a 33-frame burst (16 "
      "channels of a smoothing profile loaded by restartScript) leaves in packets cut between frames only (174 + 22 "
      "bytes) and arrives whole and in order on both (remote slot: nothing moves)", needs=["navicore", "wcb1"],
      links=["W1S1", "W2S1"])
def mae_remote_stream_exact(bench):
    """maestroWrite (NaviCore.ino:647-701) puts a type-2 slot's frame into the broadcast WCBStream: AA, the slot's
    device, the command & 0x7F, the payload. ?MAE,<slot>,<ch>,<pos> is maestroSetTarget (:3445-3449, :1033-1059), so
    6000 = 0x70 + 0x2E<<7 goes out as 70 2E (NAVICORE.md §1.2). The stream holds 177 bytes (WCB_Client WCBStream.h:104)
    and sends on a 2 ms gap at loop()'s wcb->update(); before a frame that would not fit whole maestroWrite sends what
    it holds (:668-670, :679-682), so a burst is cut between frames, never inside one, where a cut would reach the
    Maestro as garbage. The burst: a free smoothing profile given speed and accel on 16 free channels of the slot and
    loaded by restartScript,0,p<P> (applyScriptEasing, maestroApplySmoothProfile :1120-1138), 32 six-byte frames and
    the 4-byte subroutine frame in one loop() pass: stream_packets gives 174 (29 frames) + 22. NaviCore prints
    '[WCBStream] Broadcast (Kyber) <n> bytes — OK' for each packet (WCBStream.cpp _flushBuffer), the only place a
    packet's edge shows: W1 and W2 write the packets to their S1 back to back. Every config save re-applies easing and
    schedules two repeats 500 ms apart on every slot (processSwitches :2466-2478), which can put the bench knob J4's
    slot-4 channel 0 on the stream (and J4's Snappy speed on slot 2 always), so the burst waits 1.5 s after its save,
    and the first step 1.5 s after the test starts: the previous test's nc_guard restore is such a save, and a repeat's
    packet among these would miscount them. Device 4 is hosted nowhere and Maestro 2 ignores another device's frames:
    nothing moves. The profile is restored by nc_guard; no knob or action uses it."""
    l11, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    hook = _hooks(nc)
    flags = DBG_MAESTRO | (DBG_WIRE if hook else 0)
    w = Wires(nc, dev, (l11, tap), hook)
    problems = []
    time.sleep(1.5)
    with nc.debug(flags):
        lines = w.step(lambda: nc.mae_set(slot, 5, 6000), pololu(dev, CMD_TARGET, 5, 6000), f"?MAE,{slot},5,6000")
    if kyber_packets(lines) != [(6, "OK")]:
        problems.append(f"?MAE,{slot},5,6000 left in packets {kyber_packets(lines)}, expected one of 6 bytes")
    with nc_guard(bench) as g:
        nc = w.nc = g.nc
        profs = free_profiles(g.before)
        if not profs:
            raise Skip("no free smoothing profile to borrow for the burst")
        p, chans = profs[0], _free_channels(g.before, slot, 16)
        profiles = [dict(x) for x in g.before.get("smoothProfiles") or []]
        profiles[p] = {"name": "HILburst", "entries": [{"mid": slot, "ch": c, "spd": 100 + c, "acc": 10 + c}
                                                       for c in chans]}
        nc.set_config({"smoothProfiles": profiles})
        time.sleep(1.5)
        want = b"".join(pololu(dev, CMD_SPEED, c, 100 + c) + pololu(dev, CMD_ACCEL, c, 10 + c) for c in chans) + \
            pololu(dev, CMD_SUB, 0)
        with nc.debug(flags):
            lines = w.step(lambda: nc.test_action(_maestro(slot, f"restartScript,0,p{p}")), want,
                           f"restartScript,0,p{p} (33 frames)", timeout=3.0, retry=False)
        sizes = kyber_packets(lines)
        split = stream_packets(frame_lengths(want))
        bench.note(f"slot {slot} (device {dev}): the burst left in packets {sizes}; the frame-boundary rule gives {split}")
        if sizes != [(n, "OK") for n in split]:
            problems.append(f"the 33-frame burst left in packets {sizes}, expected {split} (cut between frames)")
    if w.retries:
        bench.note(f"sent again after a lost broadcast: {w.retries}")
    problems += w.problems
    assert not problems, "; ".join(problems)


@test("ncdev.mae_remote_verbs", "Every Maestro action verb on a remote slot puts its exact Pololu frames on W1 S1 and "
      "the W2 S1 tap: setTarget, setSpeed, setAccel, setSpeedAccel, goHome, stopScript, restartScript, subParam, with "
      "their clamps (target and speed 16383, subParam's parameter too), channel 32 refused, the cmd cut at 35 "
      "characters, an unknown verb silent; a legacy maestro_local action goes to slot 1 (remote slot: nothing moves)",
      needs=["navicore", "wcb1"], links=["W1S1", "W2S1"])
def mae_remote_verbs(bench):
    """executeMaestroCmd (NaviCore.ino:1307-1390) copies the action's cmd into char buf[36], so a cmd keeps 35
    characters (:1308): 'setTarget,5,' and 24 digits ending 6000 parses as 600. maestroSetTarget and maestroSetSpeed
    clamp at 16383 instead of wrapping to the low 14 bits (:1035, :1062), maestroSubParam clamps its parameter
    (:1094), maestroSetAccel sends 0-255 as two 7-bit bytes (:1071-1082), maestroChanOk refuses a channel over 31 with
    a line (:937-942). A setTarget with no position, and an unknown verb, write nothing. restartScript is given a
    profile with no entries for this slot (profile_without), so the action's own easing wins over the slot's switch
    easing and loads nothing (:1368-1384): the subroutine frame is all it writes. The frames are what WcbCmd's
    builders make (WcbMaestro.cpp:23-72) for the same values. A maestro_local action is the legacy form of slot 1
    (:2066-2072): with an unknown verb it prints its routing line and writes nothing (NAVICORE.md's low-risk
    nc.action.maestro_local_legacy), on NaviCore's local Maestro bus included ([WIRE] Serial2, hook image). The
    numbers that wrap before any check (channel 256+c, accel 256+a, a value past 65535) are
    ncdev.mae_verb_no_alias's; subroutines 128-255, ncdev.mae_subroutine_msb's. Device 4 is hosted nowhere: nothing
    moves."""
    l11, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    prof = profile_without(cfg, slot)
    if prof is None:
        raise Skip(f"every smoothing profile has entries for Maestro slot {slot}: restartScript would load easing first")
    hook = _hooks(nc)
    w = Wires(nc, dev, (l11, tap), hook)
    with nc.debug(DBG_MAESTRO | (DBG_WIRE if hook else 0)):
        for cmd, want in mae_cases(dev, prof):
            lines = w.step(lambda cmd=cmd: nc.test_action(_maestro(slot, cmd)), want, cmd)
            if f"[DISPATCH] Maestro {slot}  {cmd}" not in lines:
                w.problems.append(f"{cmd}: no '[DISPATCH] Maestro {slot}  {cmd}' trace line")
            if cmd.startswith(("setTarget,32", "setSpeed,32", "setAccel,32")) and \
                    f"[DISPATCH] Maestro {slot}: channel 32 out of range (0-31) — skipped" not in lines:
                w.problems.append(f"{cmd}: no 'channel 32 out of range' line")
        lines = w.step(lambda: nc.test_action({"type": "maestro_local", "cmd": "HILnoverb"}), b"", "maestro_local")
        if "[DISPATCH] Maestro (legacy local → ID 1)  HILnoverb" not in lines:
            w.problems.append("maestro_local: no '[DISPATCH] Maestro (legacy local → ID 1)  HILnoverb' line")
        if hook and wire_log(lines, "Serial2")[0]:
            w.problems.append(f"maestro_local with an unknown verb wrote {wire_log(lines, 'Serial2')[0].hex(' ')} to "
                              f"the local Maestro bus")
    if w.retries:
        bench.note(f"sent again after a lost broadcast: {w.retries}")
    assert not w.problems, "; ".join(w.problems)


@test("ncdev.mae_verb_no_alias", "(should) A Maestro action's number that is out of range is refused or clamped, never "
      "wrapped onto another valid one: channel 261 must not move channel 5, accel 300 must not send accel 44, target "
      "70000 must not send 4464, speed 65537 must not send speed 1, restartScript,300 must not run subroutine 44 "
      "(remote slot: nothing moves)", needs=["navicore", "wcb1"], links=["W1S1"])
def mae_verb_no_alias(bench):
    """NAVICORE.md D-NC56. executeMaestroCmd casts every number before anything checks it: (uint8_t)atoi for the
    channel, the accel and the subroutine, (uint16_t)atoi for the target, the speed and subParam's parameter
    (NaviCore.ino:1316, :1320, :1324, :1331, :1372, :1388). So 'setTarget,261,6000' reaches maestroChanOk as channel 5
    and moves it; 'setAccel,5,300' sends accel 44; 'setTarget,5,70000' arrives as 4464, under the 16383 clamp that
    maestroSetTarget's comment says exists so a value is 'capped, not wrapped' (:1035-1036); 'setSpeed,5,65537' arrives
    as speed 1, the slowest there is; 'restartScript,300' runs subroutine 44. Each is a valid-looking command for
    something else. WcbCmd's text parser has the same cast for the channel and the target (WcbMaestro.cpp:141, :147,
    :153: the range check in buildSetTarget runs on the cast value), so the inbound ;M on both firmwares shares it.
    Recommendation: parse into a long, refuse a channel, subroutine or accel out of range with a line, clamp a target or
    speed from the long. The positive control, setTarget,5,6000, must arrive first. The subroutine uses a profile with
    no entries for this slot, as ncdev.mae_remote_verbs does. Device 4 is hosted nowhere: nothing moves."""
    l11 = link(bench, 1, "S1")
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    prof = profile_without(cfg, slot)
    w = Wires(nc, dev, (l11,), _hooks(nc))
    aliased = [("setTarget,261,6000", pololu(dev, CMD_TARGET, 5, 6000), "channel 5"),
               ("setAccel,5,300", pololu(dev, CMD_ACCEL, 5, 44), "accel 44"),
               ("setTarget,5,70000", pololu(dev, CMD_TARGET, 5, 70000 & 0xFFFF), "target 4464"),
               ("setSpeed,5,65537", pololu(dev, CMD_SPEED, 5, 1), "speed 1"),
               ("subParam,3,70000", pololu(dev, CMD_SUB_P, 3, 70000 & 0xFFFF), "parameter 4464")]
    if prof is not None:
        aliased.append((f"restartScript,300,p{prof}", pololu(dev, CMD_SUB, 44), "subroutine 44"))
    wrapped = []
    with nc.debug(DBG_MAESTRO):
        w.step(lambda: nc.test_action(_maestro(slot, "setTarget,5,6000")), pololu(dev, CMD_TARGET, 5, 6000),
               "control setTarget,5,6000")
        if w.problems:
            raise AssertionError("the positive control did not reach W1 S1: " + "; ".join(w.problems))
        for cmd, alias, what in aliased:
            m = l11.mark()
            nc.test_action(_maestro(slot, cmd))
            time.sleep(0.8)
            if alias in dev_stream(l11.received(m), dev):
                wrapped.append(f"{cmd} sent {what} ({alias.hex(' ')})")
    assert not wrapped, "(should, D-NC56) out-of-range numbers wrapped onto valid ones: " + "; ".join(wrapped)


@test("ncdev.mae_subroutine_msb", "(should) A subroutine number over 127 never puts a command-range byte inside a "
      "Pololu frame: restartScript,200 and subParam,200,5 must not send 0xC8 after AA <dev> 27/28 (remote slot: "
      "nothing moves; Maestro 2's error flags are read and cleared after)", needs=["navicore", "wcb1"],
      links=["W1S1"])
def mae_subroutine_msb(bench):
    """NAVICORE.md D-NC23. maestroRestartScript writes the subroutine byte as given through WcbCmd's
    buildSubroutineFrame (NaviCore.ino:1085-1089, :668; WcbMaestro.cpp:23-29: out[3] = seq, unmasked), and
    maestroSubParam puts it in p[0] (:1093-1101), so subroutine 200 goes out as AA <dev> 27 C8: 0xC8 has its top bit
    set, which a Maestro reads as the start of a new command (a compact-protocol command byte), not as data. WcbCmd
    accepts 0-255 for the subroutine (WcbMaestro.cpp:126), so the WCB's ;M<dev>,<n> has the twin. Recommendation: refuse
    n > 127 in WcbCmd, for both firmwares (push WcbCmd first, WCB CLAUDE.md rule 1). Passing: no byte of 0x80 or more
    other than a frame's AA lead reaches W1 S1 for these commands - refused, masked or clamped. The positive control,
    restartScript,100, must arrive. Device 4 is hosted nowhere, but the same broadcast reaches Maestro 2 on W2 S1, where
    a stray command byte can set a serial error flag: they are read, and so cleared, at the end (s22
    _settle_maestro2)."""
    l11 = link(bench, 1, "S1")
    nc, w1 = _nc(bench), usb_wcb(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    prof = profile_without(cfg, slot)
    if prof is None:
        raise Skip(f"every smoothing profile has entries for Maestro slot {slot}: restartScript would load easing first")
    w = Wires(nc, dev, (l11,), _hooks(nc))
    bad = []
    try:
        with nc.debug(DBG_MAESTRO):
            w.step(lambda: nc.test_action(_maestro(slot, f"restartScript,100,p{prof}")), pololu(dev, CMD_SUB, 100),
                   "control restartScript,100")
            if w.problems:
                raise AssertionError("the positive control did not reach W1 S1: " + "; ".join(w.problems))
            for cmd in (f"restartScript,200,p{prof}", "subParam,200,5"):
                m = l11.mark()
                nc.test_action(_maestro(slot, cmd))
                time.sleep(0.8)
                got = l11.received(m)
                high = [i for i, b in enumerate(got) if b >= 0x80 and b != 0xAA]   # a frame's data bytes are all < 0x80
                if high:
                    bad.append(f"{cmd}: W1 S1 got {got.hex(' ')}, a command-range byte inside the frame")
    finally:
        _settle_maestro2(w1)
    assert not bad, "(should, D-NC23) " + "; ".join(bad)


def _easing_frames(l11, since, dev, chans):
    """The frames for Maestro `dev` W1 S1 received since `since` that are speed/accel on `chans` or a subroutine frame, as
    (cmd, ch, value) - the bench knob J4's own channel 0 on the same slot is left out."""
    return [(c, ch, v) for d, c, ch, v in pololu_frames(l11.received(since)) if d == dev and
            (c == CMD_SUB or (c in (CMD_SPEED, CMD_ACCEL) and ch in chans))]


@test("ncdev.easing_repeat_frames", "Easing on a remote slot as frames on W1 S1: setEasing,p<P> sends the knob-driven "
      "channel's speed and accel at once and again 500 and 1000 ms later (probe clock); setEasing,off sends 0/0 once "
      "and no repeat; restartScript,n loads its own profile, the switch's when it has none, the switch's over its own "
      "with ,o, zeroes every managed channel under Off, and nothing when released - each before its subroutine frame "
      "(remote slot: nothing moves)", needs=["navicore", "wcb1"], links=["W1S1"])
def easing_repeat_frames(bench):
    """setEasing (executeMaestroCmd :1336-1349) sets the slot's switch easing, re-applies it at once to every
    passthrough-knob channel on the slot (reapplyMaestroEasing :1147-1170, cache-gated) and schedules EASE_REPEATS (2)
    re-sends EASE_REPEAT_MS (500) apart (:1185-1194), which re-send only positive limits and never drive 0
    (reassertMaestroEasing :1212-1233; easingRepeatTick :1235-1250). restartScript,n[,p<N>][,o] (:1368-1384): the
    action's own profile wins, the switch's easing is the fallback and wins over it with ',o'; applyScriptEasing
    loads a profile's speed and accel on every channel it names (maestroApplySmoothProfile :1120-1131), or with Off
    zeroes every channel any profile manages (maestroResetSmoothedChannels :1106-1116), then the subroutine frame. In
    nc_guard: two free profiles P and Q, P naming channels X and Y, Q naming X (speeds random per run, so a cache left
    by an earlier run cannot hide a write), and a free knob on SBUS channel 0 - never dispatched (processKnobs skips
    it) yet counted by the re-apply - driving X with no profile of its own, so X follows the switch. Frames of the bench
    knob J4 on the slot's channel 0 are left out. The slot's easing ends released, as it began (RAM only). Device 4 is
    hosted nowhere: nothing moves."""
    l11 = link(bench, 1, "S1")
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        knobs, profs = _free_knobs(g.before), free_profiles(g.before)
        if not knobs or len(profs) < 2:
            raise Skip("needs a free knob and two free smoothing profiles to borrow")
        x, y = _free_channels(g.before, slot, 2)
        p, q = profs[:2]
        sp, ap, sq, aq = random.randint(100, 300), random.randint(5, 40), random.randint(301, 500), random.randint(41, 80)
        sy, ay = random.randint(100, 500), random.randint(5, 80)
        profiles = [dict(v) for v in g.before.get("smoothProfiles") or []]
        profiles[p] = {"name": "HILp", "entries": [{"mid": slot, "ch": x, "spd": sp, "acc": ap},
                                                   {"mid": slot, "ch": y, "spd": sy, "acc": ay}]}
        profiles[q] = {"name": "HILq", "entries": [{"mid": slot, "ch": x, "spd": sq, "acc": aq}]}
        nc.set_config({"smoothProfiles": profiles, "knobs": {knobs[0]: _knob(0, outputs=[_out(x, target=slot)])}})
        managed = managed_channels(nc.config(), slot)
        time.sleep(1.5)                           # the save's own re-apply and its two repeats
        chans = {x, y}
        P = [(CMD_SPEED, x, sp), (CMD_ACCEL, x, ap)]
        Py = [(CMD_SPEED, y, sy), (CMD_ACCEL, y, ay)]
        Q = [(CMD_SPEED, x, sq), (CMD_ACCEL, x, aq)]
        zero = [(CMD_SPEED, x, 0), (CMD_ACCEL, x, 0)]

        def act(cmd, wait):
            m = l11.mark()
            nc.test_action(_maestro(slot, cmd))
            time.sleep(wait)
            return m, _easing_frames(l11, m, dev, chans)

        def expect(what, got, want):
            if got != want:
                problems.append(f"{what}: W1 S1 got {got}, expected {want}")

        with nc.debug(DBG_MAESTRO):
            m, got = act(f"setEasing,p{p}", 1.6)
            expect(f"setEasing,p{p}", got, P * 3)
            t = [_probe_ms(l11, m, pololu(dev, CMD_SPEED, x, sp), nth=i) for i in range(3)]
            if None not in t:
                notes.append(f"repeats at +{t[1] - t[0]} and +{t[2] - t[0]} ms")
                if not (400 <= t[1] - t[0] <= 650 and 900 <= t[2] - t[0] <= 1200):
                    problems.append(f"setEasing,p{p}: the repeats came {t[1] - t[0]} and {t[2] - t[0]} ms after the "
                                    f"first send, expected 500 and 1000")
            m, got = act("setEasing,off", 1.6)
            expect("setEasing,off (0/0 once, never repeated)", got, zero)
            m, got = act("setEasing,release", 1.3)
            expect("setEasing,release (already 0: nothing)", got, [])
            m, got = act("restartScript,7", 0.8)
            expect("restartScript,7, released", got, [(CMD_SUB, 7, None)])
            m, got = act(f"restartScript,7,p{p}", 0.8)
            expect(f"restartScript,7,p{p}", got, P + Py + [(CMD_SUB, 7, None)])
            act(f"setEasing,p{q}", 1.6)
            m, got = act("restartScript,7", 0.8)
            expect(f"restartScript,7 under setEasing,p{q}", got, Q + [(CMD_SUB, 7, None)])
            m, got = act(f"restartScript,7,p{p}", 0.8)
            expect(f"restartScript,7,p{p} under p{q} (its own wins)", got, P + Py + [(CMD_SUB, 7, None)])
            m, got = act(f"restartScript,7,p{p},o", 0.8)
            expect(f"restartScript,7,p{p},o under p{q} (the switch overrides)", got, Q + [(CMD_SUB, 7, None)])
            act("setEasing,off", 1.6)
            m = l11.mark()
            nc.test_action(_maestro(slot, "restartScript,7"))
            time.sleep(0.8)
            reset = [(c, ch, v) for d, c, ch, v in pololu_frames(l11.received(m)) if d == dev]
            want = [f for ch in managed for f in ((CMD_SPEED, ch, 0), (CMD_ACCEL, ch, 0))] + [(CMD_SUB, 7, None)]
            expect("restartScript,7 under setEasing,off (every managed channel zeroed)", reset, want)
            act("setEasing,release", 1.3)
    bench.note(f"slot {slot} (device {dev}) ch {x}/{y}: P=p{p} {sp}/{ap}, Q=p{q} {sq}/{aq}; " + "; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncdev.mae_remote_read", "A remote slot's ?MAE query goes out as the ;M<dev>,<verb> text, the WCB that hosts the "
      "Maestro reads it (the request frame on the W2 S1 tap) and answers :MQR, and NaviCore prints [MAE:<slot>] when it "
      "lands, with W2's own copy of the value; asked through W1, the late marker comes back as [TERM:20][MAE:<slot>] "
      "(reads only: getErrors clears Maestro 2's error flags)", needs=["navicore", "wcb1"], links=["W2S1"])
def mae_remote_read(bench):
    """maestroLocalQuery on a type-2 slot broadcasts ';M<dev>,getPosition,<ch>' / getMovingState / getErrors as text
    (maestroBroadcastReadVerb, NaviCore.ino:703-723, :725-745) and prints nothing yet (maestroReportQuery returns on -3,
    :753-754). W1 drops a mesh get for a device it does not host (WCB.ino maestroRewriteInboundGet, then
    handleMaestroGet's lastReceivedViaESPNOW return, WCB_Maestro.cpp:563-566); W2 hosts Maestro 2, reads it on S1 (the
    request frame, AA 02 10 <ch>, is on the tap), stores m2pos<ch>/m2moving/m2err in RAM and, NaviCore being its
    controller (?CONTROLLER,ON,20), answers ':MQR,2,<ch>,POS|MOV|ERR,<value>' (WCB_Maestro.cpp:521-627).
    maeConsumeRemoteReply files it under the slot that carries device 2 (:808-827) and maePumpRemoteEmits prints
    [MAE:<slot>]{...} from loop() (:829-850). Asked over the bridge (';W20,?MAE,...' typed on W1), the read latches the
    relay (maeLatchRemoteRelay :802-804) and the marker is mirrored to W1 as [TERM:20]... when it lands. The plan named
    slot 2 = device 2; this uses whichever remote slot's device a WCB hosts (s41 _hosted_remote_slot), Maestro 2 on W2
    here. NaviCore's docs/TROUBLESHOOTING.md:55 still says the WCB-side read has not shipped: this shows it has. The
    RAM variables are cleared on W2 afterwards."""
    tap = link(bench, 2, "S1")
    nc, w1 = _nc(bench), usb_wcb(bench)
    cfg = nc.config()
    slot, dev, host = _hosted_remote_slot(nc, cfg)
    nid = nc.wcb_status()["self"]
    problems, notes = [], []
    names = (f"m{dev}pos0", f"m{dev}moving", f"m{dev}err")
    with Console(bench, host) as ch:
        def var(name):
            m = ch.send(f"?VAR,GET,{name}")
            try:
                return int(ch.expect(rf"^\[VAR\] {name} = (-?\d+)", timeout=3, since=m).group(1))
            except AssertionError:
                return None
        try:
            with nc.debug(DBG_MAESTRO):
                for q, cli, frame, name in (("pos", f"?MAE,GET,{slot},0", pololu(dev, 0x10, 0), names[0]),
                                            ("mov", f"?MAE,MOVING,{slot}", pololu(dev, 0x13), names[1]),
                                            ("err", f"?MAE,ERR,{slot}", pololu(dev, 0x21), names[2])):
                    mt, nm = tap.mark(), nc.dev.mark()
                    nc.dev.send(cli)
                    got = _await_mae(nc, nm, slot, q, 0 if q == "pos" else None)
                    if not isinstance(got, int):
                        problems.append(f"{cli}: no [MAE:{slot}] {q} marker with a value within 4 s ({got})")
                        continue
                    if frame not in tap.received(mt):
                        problems.append(f"{cli}: W{host} S1 never sent the request {frame.hex(' ')} (tap got "
                                        f"{tap.received(mt).hex(' ') or 'nothing'})")
                    own = var(name)
                    notes.append(f"{q}={got} (W{host} {name}={own})")
                    if own != got:
                        problems.append(f"{cli}: NaviCore printed {got}, W{host} stored {name}={own}")
                wm, nm = w1.dev.mark(), nc.dev.mark()
                w1.send(f";W{nid},?MAE,GET,{slot},0")
                try:
                    w1.dev.expect(rf'^\[TERM:{nid}\]\[MAE:{slot}\]\{{"q":"pos","ch":0,"val":\d+\}}', timeout=5, since=wm)
                except AssertionError:
                    problems.append(f"';W{nid},?MAE,GET,{slot},0' on W1: no [TERM:{nid}][MAE:{slot}] marker came back")
                if not isinstance(_await_mae(nc, nm, slot, "pos", 0, timeout=2.0), int):
                    problems.append("the relayed read printed no marker on NaviCore's own console")
        finally:
            for name in names:
                ch.send(f"?VAR,CLEAR,{name}")
                time.sleep(0.2)
    bench.note(f"slot {slot} = device {dev} on W{host}: " + ", ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ NaviCore's own Maestro 1 (moves a real channel)
def _local_channel(nc, cfg):
    """(slot, device, channel, p0, base) for the local-Maestro tests: the lowest channel of a local slot that no
    passthrough knob drives and that answers ?MAE,GET (NaviCore.undriven_channel), the one
    navicore.maestro_mesh_settarget_readback already moves on every run (ch 1 here). It must read 0 - off, the Maestro
    sends it no pulses, as every undriven dome channel did in run 20260928-204259 - or a servo position inside
    4400-7600, so a move of 100-400 quarter-us around `base` stays in range and the test can put it back exactly: base
    is 6000 for an off channel, else p0. Skip otherwise."""
    slot, dev, ch, p0 = nc.undriven_channel(cfg)
    if not (p0 == 0 or 4400 <= p0 <= 7600):
        raise Skip(f"Maestro slot {slot} ch {ch} reads {p0}: neither off nor a servo position these moves stay inside")
    return slot, dev, ch, p0, (6000 if p0 == 0 else p0)


def _read_until(nc, slot, ch, want, timeout=1.5):
    """?MAE,GET until it reads `want` or `timeout` passes -> the last reading."""
    deadline = time.monotonic() + timeout
    while True:
        got = nc.mae_get(slot, ch)
        if got == want or time.monotonic() >= deadline:
            return got
        time.sleep(0.1)


def _still(nc, slot, timeout=4.0):
    """?MAE,MOVING until it reads 0 -> seconds it took, or None when it never did."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if nc.mae_moving(slot) == 0:
            return time.monotonic() - t0
        time.sleep(0.1)
    return None


def _put_back(nc, slot, ch, p0):
    """Speed and accel 0 (the Maestro's 'no limit', which ?MAE,FREE and ncengine.maestro_skip_running_slot also leave),
    then the channel's starting target: 0 turns it off again."""
    nc.mae_free(slot, ch)
    nc.mae_set(slot, ch, p0)


@test("ncdev.mae_local_frames_readback", "NaviCore's own Maestro 1 takes the frames NaviCore writes and reads them "
      "back: ?MAE,FREE zeroes a channel's speed and accel, ?MAE,<slot>,<ch>,<pos> and a Maestro action set targets "
      "?MAE,GET reads back exactly, channel 32 is refused before the bus, target 0 reads back 0 (off), the error "
      "register stays 0; the bus bytes match on a hook image (moves one dome channel 6000-6200 and back: servos.py)",
      needs=["navicore"], links=[])
def mae_local_frames_readback(bench):
    """A local (type 1) slot's frames go to Serial2 (maestroWrite, NaviCore.ino:647-701), the same bytes a remote slot
    puts in the WCBStream; ?MAE,GET/MOVING/ERR read the Maestro's reply off Serial2 synchronously, within 25 ms
    (maestroLocalQuery :725-745). ?MAE,FREE is maestroSetSpeed and maestroSetAccel with 0 (:3412-3416), each logged
    '[DISPATCH] Maestro <slot> ch <ch>  SetSpeed 0' (:1060-1082). ?MAE,<slot>,<ch>,<pos> is maestroSetTarget
    (:3445-3449); a Maestro action on the slot goes through executeMaestroCmd (:1307-1316) to the same function.
    maestroChanOk refuses channel 32 with a line and writes nothing (:937-942). Target 0 stops the pulses, and the
    Maestro then reads 0 (the plan's 'a released channel reads 0', checked here and in ncdev.knob_local_readback).
    ?MAE,ERR reads and clears the error register (:3436-3444): read first, then it must still read 0 at the end. The
    plan's 'a target of 16384 reads 16383' is left to the remote slot (ncdev.mae_remote_verbs, as bytes): on a real
    channel the Maestro clamps a target to that channel's own range, so it would read that range's end, after a
    full-travel move. What moves: the channel _local_channel picks (see there), to 6000-6200 or p0..p0+200 and back to
    where it was, with speed and accel 0 as ncengine.maestro_skip_running_slot leaves them."""
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev, ch, p0, base = _local_channel(nc, cfg)
    hook = _hooks(nc)
    problems = []

    def bus(lines, want, what):
        """NaviCore's Serial2 frames for this channel must be `want`, byte-exact: a knob or an easing repeat after a
        config save can write another channel of the same Maestro meanwhile."""
        if not hook:
            return
        got, gaps = wire_log(lines, "Serial2")
        mine = b"".join(pololu(d, c, x, v) for d, c, x, v in pololu_frames(got) if x == ch)
        if gaps:
            problems.append(f"{what}: NaviCore's [WIRE] log for Serial2 lost lines ({'; '.join(gaps)})")
        elif mine != want:
            problems.append(f"{what}: NaviCore wrote {mine.hex(' ') or 'nothing'} to Serial2 for ch {ch}, expected "
                            f"{want.hex(' ')}")
    e0 = nc.mae_err(slot)
    try:
        with nc.debug(DBG_MAESTRO | (DBG_WIRE if hook else 0)):
            lines = [x.rstrip() for x in nc.mae_free(slot, ch)]
            for word in ("SetSpeed", "SetAccel"):
                if f"[DISPATCH] Maestro {slot} ch {ch}  {word} 0" not in lines:
                    problems.append(f"?MAE,FREE: no '[DISPATCH] Maestro {slot} ch {ch}  {word} 0' line")
            bus(lines, pololu(dev, CMD_SPEED, ch, 0) + pololu(dev, CMD_ACCEL, ch, 0), "?MAE,FREE")
            for how, pos in (("?MAE", base), ("?MAE", base + 100), ("action", base + 200)):
                if how == "?MAE":
                    lines = [x.rstrip() for x in nc.mae_set(slot, ch, pos)]
                else:
                    nm = nc.dev.mark()
                    nc.test_action(_maestro(slot, f"setTarget,{ch},{pos}"))
                    lines = _trace(nc, nm, 0.1)
                bus(lines, pololu(dev, CMD_TARGET, ch, pos), f"{how} target {pos}")
                got = _read_until(nc, slot, ch, pos)
                if got != pos:
                    problems.append(f"{how} target {pos}: ?MAE,GET,{slot},{ch} reads {got}")
            lines = [x.rstrip() for x in nc.cli(f"?MAE,{slot},32,6000")]
            if f"[DISPATCH] Maestro {slot}: channel 32 out of range (0-31) — skipped" not in lines:
                problems.append("?MAE with channel 32: no 'channel 32 out of range' line")
            ch32 = [f for f in pololu_frames(wire_log(lines, "Serial2")[0]) if f[2] == 32] if hook else []
            if ch32:
                problems.append(f"channel 32 still wrote {ch32} to the Maestro bus")
            lines = [x.rstrip() for x in nc.mae_set(slot, ch, p0)]
            bus(lines, pololu(dev, CMD_TARGET, ch, p0), f"target {p0} (back)")
            got = _read_until(nc, slot, ch, p0)
            if got != p0:
                problems.append(f"back to {p0}: ?MAE,GET reads {got}")
        moving, err = nc.mae_moving(slot), nc.mae_err(slot)
        if moving != 0:
            problems.append(f"?MAE,MOVING,{slot} reads {moving} with nothing moving")
        if err != 0:
            problems.append(f"the Maestro's error register reads {err} after the test (it read {e0} before)")
    finally:
        _put_back(nc, slot, ch, p0)
    bench.note(f"slot {slot} (device {dev}) ch {ch}: started at {p0}, moved around {base}; errors before {e0}")
    assert not problems, "; ".join(problems)


@test("ncdev.mae_local_easing_timing", "A speed limit on NaviCore's own Maestro 1 shows while the servo travels: 150 ms "
      "into a 400 quarter-us move at speed 4 ?MAE,MOVING reads 1 and ?MAE,GET a position strictly between the ends; "
      "it arrives about a second later, exactly (moves one dome channel 6000-6400, slowly, and back: servos.py)",
      needs=["navicore"], links=[])
def mae_local_easing_timing(bench):
    """setSpeed goes out through maestroSetSpeed (NaviCore.ino:1060-1070); a Maestro speed of S moves S x 0.25 us every
    10 ms, 400 quarter-us a second at 4, so the step takes about a second. getMovingState and getPosition are read off
    Serial2 as the servo travels (maestroLocalQuery :725-745): the plan's check that NaviCore's reads see the easing it
    set, not only the end point. The channel is off at rest here, and a channel switched on by a target jumps to it at
    once (a speed limit applies from its next target), so it is first put at the base with speed 0. What moves: the
    channel _local_channel picks, base -> base+400 at speed 4 and back, then speed and accel 0 and its starting
    target (off again when it was off)."""
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev, ch, p0, base = _local_channel(nc, cfg)
    problems, notes = [], []
    try:
        nc.mae_free(slot, ch)
        nc.mae_set(slot, ch, base)
        if _read_until(nc, slot, ch, base) != base or _still(nc, slot) is None:
            raise AssertionError(f"slot {slot} ch {ch} never settled at {base} with speed 0")
        nc.test_action(_maestro(slot, f"setSpeed,{ch},4"))
        for a, b in ((base, base + 400), (base + 400, base)):
            t0 = time.monotonic()
            nc.mae_set(slot, ch, b)
            time.sleep(max(0.0, 0.15 - (time.monotonic() - t0)))
            moving, mid = nc.mae_moving(slot), nc.mae_get(slot, ch)
            still = _still(nc, slot, timeout=4.0)
            took = None if still is None else time.monotonic() - t0
            end = nc.mae_get(slot, ch)
            notes.append(f"{a}->{b}: MOVING {moving}, GET {mid} early; still after {took and round(took, 2)} s, at {end}")
            if moving != 1:
                problems.append(f"{a}->{b}: ?MAE,MOVING read {moving} during the move")
            if not (isinstance(mid, int) and min(a, b) < mid < max(a, b)):
                problems.append(f"{a}->{b}: ?MAE,GET read {mid} during the move, not a position between the ends")
            if end != b:
                problems.append(f"{a}->{b}: it came to rest at {end}")
            if took is None or not 0.6 <= took <= 2.0:
                problems.append(f"{a}->{b}: the move took {took} s at speed 4 (about 1 s expected)")
    finally:
        _put_back(nc, slot, ch, p0)
    bench.note(f"slot {slot} ch {ch}: " + "; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncdev.knob_local_readback", "A passthrough knob on NaviCore's own Maestro 1 sets the position ?MAE,GET reads "
      "back - posMin + (v-172)(posMax-posMin)/1639, exactly - and its idle release turns the channel off, which reads "
      "0, until the stick moves again (moves the rx stick; a borrowed knob drives one dome channel 5800-6200: "
      "servos.py)", needs=["navicore", "sbus"], links=[])
def knob_local_readback(bench):
    """The local variant of sbus.knob_passthrough_remote and sbus.knob_auto_release (NAVICORE.md nc.map.knob_passthrough,
    nc.mae.auto_release): processKnobs maps the stick with sbusToRange (NaviCore.ino:593-612, :2598-2700; s42
    knob_pos) and writes maestroSetTarget on the knob's slot, here the local Maestro 1 (Serial2), which ?MAE,GET reads
    back. maestroIdleReleaseTick sends target 0 once the channel has been idle releaseIdleMs (:2731-2752); the next stick
    move writes a real target again (:1033-1058). A free knob is borrowed inside nc_guard, on the controller's rx stick
    (bound to nothing, s42 _stick), with one output on the channel _local_channel picks and a range of base +-200, so the
    stick's centre gives the base exactly. The guard restores the knob, and its restore resets the release state
    (resetMaestroReleaseState); the channel is then put back to its starting target (off here). What moves: the rx
    stick (re-emitted on SBUS OUT, and the controller's own RC PWM 1) and that one channel within 5800-6200, off for a
    second or two after each idle release."""
    ctl, nc, scfg, ncfg = _sbus_setup(bench)
    ch_sbus = _stick(nc, scfg, ncfg, "rx")
    slot, dev, ch, p0, base = _local_channel(nc, ncfg)
    sticks = Sticks(ctl, scfg)
    lo, hi, rel = base - 200, base + 200, 1500
    problems, notes = [], []
    try:
        with nc_guard(bench) as g:
            nc = g.nc
            knobs = _free_knobs(g.before)
            if not knobs:
                raise Skip("no free knob to borrow")
            nc.set_config({"knobs": {knobs[0]: _knob(ch_sbus, outputs=[_out(ch, lo, hi, target=slot,
                                                                            releaseIdleMs=rel)])}})
            time.sleep(1.5)
            for v in (1400, 600):
                sticks.set("rx", v)
                want = knob_pos(v, lo, hi)
                got = _read_until(nc, slot, ch, want)
                notes.append(f"{v} -> {got}")
                if got != want:
                    problems.append(f"stick {v}: ?MAE,GET reads {got}, expected {want}")
            time.sleep(rel / 1000 + 0.5)
            off = nc.mae_get(slot, ch)
            notes.append(f"idle {rel} ms -> {off}")
            if off != 0:
                problems.append(f"{rel} ms after the last move ?MAE,GET reads {off}, not 0 (the idle release)")
            sticks.center()
            want = knob_pos(axis_value(scfg, "rx", 0.0), lo, hi)
            got = _read_until(nc, slot, ch, want)
            if got != want:
                problems.append(f"the stick back at centre reads {got}, expected {want} (re-energised)")
    finally:
        sticks.center()
        time.sleep(0.3)
        _put_back(_nc(bench), slot, ch, p0)
    back = _nc(bench).mae_get(slot, ch)
    bench.note(f"slot {slot} ch {ch}, knob range {lo}-{hi}: " + ", ".join(notes) + f"; left at {back}")
    if back != p0:
        problems.append(f"the channel reads {back} at the end, not its starting {p0}")
    assert not problems, "; ".join(problems)


# ============================================================ devices through a WCB
def _set_dest(g, key, want):
    """Save `want` as NaviCore's <key> destination inside nc_guard `g`, unless it already is: a save rewrites /config.json
    for nothing otherwise."""
    cur = g.before.get(key) or {}
    if any(cur.get(k) != v for k, v in want.items()):
        g.nc.set_config({key: want})
        time.sleep(0.3)


def _device_step(l, nc, action, want, trace, what, timeout=2.5):
    """TEST_ACTION `action`; then wire `l` must carry exactly `want` (nothing, when it is empty) and NaviCore's trace the
    line `trace` -> problems. Steps are 0.35 s apart at least, past the WCB's 150 ms PlayWAV debounce."""
    m, nm = l.mark(), nc.dev.mark()
    nc.test_action(action)
    if want:
        try:
            l.expect(want, timeout=timeout, since=m)
        except AssertionError:
            pass
        time.sleep(0.35)
    else:
        time.sleep(0.9)
    got, lines = l.received(m), _trace(nc, nm, 0.05)
    out = [f"{what}: {l.key} got {got!r}, expected {want!r}"] if got != want else []
    if trace not in lines:
        out.append(f"{what}: NaviCore's trace lacks {trace!r}")
    return out


@test("ncdev.hcr_remote_verbs", "Every HCR action NaviCore sends a WCB lands on W2's HCR port as the bytes W2 writes "
      "for that ;H command typed on W1: the readable verbs, the numeric form for V PlayWAV/StopWAV and fn 20/21, VOLUP "
      "and VOLDN on one channel or all, instant fades on A/B; emotion 4, a fade on V, fn 1, SetEmotion 100 and "
      "SetVolume 101 are refused before anything is sent", needs=["navicore", "wcb1"], links=["W2S4"])
def hcr_remote_verbs(bench):
    """executeHcrAction's WCB branch (NaviCore.ino:1611-1650) runs hcrNormalizeAction - WcbCmd's HcrCodec::normalize, the
    same check the local transport makes (WcbHcr.cpp:7-43) - then hcrFormatWcbCommand (:1499-1555) turns (fn, chan,
    track) into a readable ;H verb, or ;H,FN,<fn>,<chan>,<track> where there is none: PlayWAV and StopWAV on V
    (:1534-1537) and the graceful stops 20/21, which fall to the default branch its comment calls unreachable (:1551).
    Fades go as ;H,FADEIN/FADEOUT on A or B only (:1628-1635); W2 runs its own HcrFade. The ;H command goes by ETM unicast
    to hcrDest's WCB, printed as '[DISPATCH] HCR→WCB2  <cmd>  OK'. W2's HCR is s15's fixture on its S4 (?HCR,PORT,
    S4:9600, the poll off; config_guard puts W1 and W2 back), and each expected byte string is the one s15 asserts for the
    same verb typed on W1 (hcr.verbs_emote_raw, hcr.verbs_audio_volume, hcr.fn_codec). The table starts by setting V, A
    and B to 40, so the steps that follow read W2's volume shadow from a known point. hcrDest is set to W2 inside
    nc_guard when the bench has it elsewhere. The refused ones print their skip line and send nothing (D-NC20 is their
    ok:true)."""
    s4 = link(bench, 2, "S4")
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        _set_dest(g, "hcrDest", {"transport": "wcb", "target": "2", "wcbPort": 1})
        with _hcr_w2(bench), nc.debug(DBG_HCR):
            for fn, chan, track, verb, want in HCR_CASES:
                problems += _device_step(s4, nc, hcr(fn, chan, track), want, f"[DISPATCH] HCR→WCB2  {verb}  OK",
                                         f"hcr {fn}/{chan}/{track}")
            for fn, chan, track, kind in HCR_REFUSED:
                problems += _device_step(s4, nc, hcr(fn, chan, track), b"", hcr_refusal("WCB", fn, chan, track, kind),
                                         f"hcr {fn}/{chan}/{track} (refused)")
    assert not problems, "; ".join(problems)


@test("ncdev.mp3_remote", "Every MP3 Trigger action NaviCore sends a WCB lands on W2's MP3 port as the bytes W2 writes for "
      "that ;A command: the volume before each play, 5-step VOLUP/VOLDN; out-of-range tracks and volumes and unknown "
      "functions are refused before anything is sent", needs=["navicore", "wcb1"], links=["W2S5"])
def mp3_remote(bench):
    """mp3FormatCommand (NaviCore.ino:1734-1760) makes the ;A verb for both transports with one set of ranges; the WCB
    branch unicasts it to mp3Dest's WCB ('[DISPATCH] MP3→WCB2  <cmd>  OK', :1823-1824) and refuses an empty one with
    'MP3: bad fn=<fn> — skipped' (:1813; the message says fn for any refusal). W2's MP3 Trigger is s15's fixture on
    its S5 at volume 20 (s15 _w2_audio: W1 learns the route and unlearns it, config_guard puts both back), and the bytes
    are Mp3Codec's (WcbMp3.cpp:32-95), as mp3.config_bytes_propagation asserts them for ;A typed on W1. mp3Dest is set
    to W2 inside nc_guard (the bench has it off)."""
    s5 = link(bench, 2, "S5")
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        _set_dest(g, "mp3Dest", {"transport": "wcb", "target": "2"})
        with _w2_audio(bench, "MP3", "S5:9600:V20", "S5"), nc.debug(DBG_MP3):
            for fn, track, verb, hexb, _ in MP3_CASES:
                problems += _device_step(s5, nc, {"type": "mp3", "fn": fn, "track": track}, bytes.fromhex(hexb),
                                         f"[DISPATCH] MP3→WCB2  {verb}  OK", f"mp3 {fn}/{track}")
            for fn, track in MP3_REFUSED:
                problems += _device_step(s5, nc, {"type": "mp3", "fn": fn, "track": track}, b"",
                                         f"[DISPATCH] MP3: bad fn={fn} — skipped", f"mp3 {fn}/{track} (refused)")
    assert not problems, "; ".join(problems)


@test("ncdev.dfp_remote", "Every DFPlayer action NaviCore sends a WCB lands on W2's DFPlayer port as the 10-byte frame W2 "
      "writes for that ;D command, DEVICE 2 included (7E FF 06 09 00 00 02 FE F0 EF); out-of-range folders, tracks, "
      "volumes, EQ and device numbers and unknown functions are refused before anything is sent",
      needs=["navicore", "wcb1"], links=["W2S5"])
def dfp_remote(bench):
    """dfpFormatCommand (NaviCore.ino:1838-1880) makes the ;D verb with the bounds DfPlayerCodec::handle checks, and the
    WCB branch unicasts it to dfpDest's WCB ('[DISPATCH] DFP→WCB2  <cmd>  OK', :1946-1947; 'DFP: bad fn=<fn> — skipped'
    for an empty one, :1936). The frames are DfPlayerCodec's (WcbDfPlayer.cpp:18-32, :51-171), as dfp.config_frames asserts
    them for ;D typed on W1, VOLUP/VOLDN absolute in steps of 2; ;D,DEVICE,2 is the WCB tracker #21 regression (the
    'D,' prefix once ate DEVICE's own D; fixed in WcbCmd 0.9.1). W2's DFPlayer is s15's fixture on its S5 at volume 20
    (W1 learns the route and unlearns it; config_guard puts both back); dfpDest is set to W2 inside nc_guard (the bench
    has it off)."""
    s5 = link(bench, 2, "S5")
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        _set_dest(g, "dfpDest", {"transport": "wcb", "target": "2"})
        with _w2_audio(bench, "DFP", "S5", "S5"), nc.debug(DBG_DFP):
            for fn, chan, track, verb, want in DFP_CASES:
                problems += _device_step(s5, nc, {"type": "dfplayer", "fn": fn, "chan": chan, "track": track}, want,
                                         f"[DISPATCH] DFP→WCB2  {verb}  OK", f"dfplayer {fn}/{chan}/{track}")
            for fn, chan, track in DFP_REFUSED:
                problems += _device_step(s5, nc, {"type": "dfplayer", "fn": fn, "chan": chan, "track": track}, b"",
                                         f"[DISPATCH] DFP: bad fn={fn} — skipped", f"dfplayer {fn}/{chan}/{track} (refused)")
    assert not problems, "; ".join(problems)


def _remote_wled(bench, nc, cfg):
    """(WLED id, its W2 token) for the WLED NaviCore routes to W2 while W2 hosts it on its S2, or Skip."""
    slot = next((w for w in cfg.get("wledSlots") or [] if w.get("configured") and w.get("wcb") == 2), None)
    if slot is None:
        raise Skip("NaviCore routes no WLED id to W2 (wledSlots)")
    wid = slot.get("id")
    tok = next((t for t in snapshot(bench, 2) if re.fullmatch(rf"\?WLED,{wid}:W2S2:\d+", t)), None)
    if tok is None:
        raise Skip(f"W2 does not host WLED {wid} on its S2")
    return wid, tok


@test("ncdev.wled_remote", "Every WLED verb NaviCore forwards to the WCB that hosts the WLED lands on W2 S2 as the JSON "
      "W2 writes for that ;L<id> command typed on W1; an unknown verb is forwarded and W2 writes nothing; id 10, a bare "
      ";L with no local WLED and a command that is not ;L are refused on NaviCore", needs=["navicore", "wcb1"],
      links=["W2S2"])
def wled_remote(bench):
    """executeWledAction (NaviCore.ino:1960-2020) reads the id after ;L, finds its wledSlots entry and, for a remote one,
    forwards the whole ';L<id>,...' command to that WCB ('[DISPATCH] WLED <id>→WCB<n>  <cmd>  OK', :2011-2014); W2's WLED
    router writes WcbWled's JSON plus a newline on its S2 (s09 wled.verbs, whose table this uses). An id over 9 is refused
    (:1972-1975), a bare ;L acts on the lowest-id LOCAL slot only (:1980-1987), and a command without L after an
    optional ';' is not a WLED command (:1962-1967). Uses the bench's own WLED 1 on W2 S2 and NaviCore's slot routing it
    there; nothing is configured, and config_guard proves W2 unchanged."""
    s2 = link(bench, 2, "S2")
    nc = _nc(bench)
    cfg = nc.config()
    wid, _ = _remote_wled(bench, nc, cfg)
    local = any(w.get("configured") and w.get("wcb") == 0 and w.get("port") in (3, 4, 5) for w in cfg.get("wledSlots") or [])
    problems = []
    with config_guard(bench, 2), nc.debug(DBG_WLED):
        for verb, want in list(WLED_VERBS) + [("BOGUS", b"")]:
            problems += _device_step(s2, nc, {"type": "wled", "cmd": f";L{wid},{verb}"}, want,
                                     f"[DISPATCH] WLED {wid}→WCB2  ;L{wid},{verb}  OK", f";L{wid},{verb}")
        refused = [(";L10,ON", "[DISPATCH] WLED: id 10 out of range (1-9) — skipped"),
                   ("HILnotL", "[DISPATCH] WLED: 'HILnotL' is not a ;L command — skipped")]
        if not local:
            refused.append((";L,ON", "[DISPATCH] WLED: bare ;L but no LOCAL WLED configured — skipped"))
        for cmd, line in refused:
            problems += _device_step(s2, nc, {"type": "wled", "cmd": cmd}, b"", line, f"{cmd} (refused)")
    assert not problems, "; ".join(problems)


@test("ncdev.wled_forward_normalised", "(should) A WLED action written without its leading ';' (L1,ON), which NaviCore "
      "routes by the id after the L, reaches a WCB-hosted WLED as it reaches a local one: W2's WLED gets "
      "{\"on\":true}, and the text 'L1,ON' is not printed out W2's broadcast ports", needs=["navicore", "wcb1"],
      links=["W2S2", "W2S3"])
def wled_forward_normalised(bench):
    """NAVICORE.md D-NC60. executeWledAction skips an optional ';' to find the L and the id (NaviCore.ino:1962-1977), so
    'L1,ON' routes like ';L1,ON': on a local slot WcbWled::emit gets the verb body and writes {"on":true} (:2009); on a
    remote slot the command is forwarded as written (:2013), and a WCB runs a unicast without its command character as
    plain broadcast text (WCB.ino:6010-6020, processBroadcastCommand): its WLED gets nothing and 'L1,ON' goes out every
    port with broadcast output on (W2's S3-S5, probe wires). The same saved action works or not by where the WLED is.
    Recommendation: forward ';L<id>,<body>' rebuilt from what was parsed. The positive control, ';L1,ON', must arrive
    first. Uses the bench's WLED 1 on W2 S2; nothing is configured."""
    s2, s3 = link(bench, 2, "S2"), link(bench, 2, "S3")
    nc = _nc(bench)
    wid, _ = _remote_wled(bench, nc, nc.config())
    with nc.debug(DBG_WLED):
        bad = _device_step(s2, nc, {"type": "wled", "cmd": f";L{wid},ON"}, b'{"on":true}\n',
                           f"[DISPATCH] WLED {wid}→WCB2  ;L{wid},ON  OK", "control")
        if bad:
            raise AssertionError("the positive control failed: " + "; ".join(bad))
        m2, m3 = s2.mark(), s3.mark()
        nc.test_action({"type": "wled", "cmd": f"L{wid},ON"})
        try:
            s2.expect(b'{"on":true}\n', timeout=2.5, since=m2)
        except AssertionError:
            pass
        time.sleep(0.5)
    problems = []
    if s2.received(m2) != b'{"on":true}\n':
        problems.append(f"'L{wid},ON' reached W2's WLED as {s2.received(m2)!r}, where ';L{wid},ON' gives "
                        f"{{\"on\":true}}")
    if f"L{wid},ON".encode() in s3.received(m3):
        problems.append(f"W2 printed 'L{wid},ON' out its S3 as broadcast text")
    assert not problems, "(should, D-NC60) " + "; ".join(problems)


# ============================================================ devices on NaviCore's own ports (opt-in navicore_aux_tx)
def _local_hcr_step(nc, fn, chan, track, want, port="S4"):
    """TEST_ACTION an HCR action with the HCR on NaviCore's own `port` -> problems: the DBG_WIRE copy must be `want`, and
    the dispatch trace must show the same payload (or, for a fade, its start line)."""
    nm = nc.dev.mark()
    nc.test_action(hcr(fn, chan, track))
    lines = _trace(nc, nm, 0.3)
    where = f"local hcr {fn}/{chan}/{track}"
    out = _wire_problems(where, lines, port, want)
    if fn in (12, 15):
        start = f"[DISPATCH] HCR→{port}  Fade{'In' if fn == 12 else 'Out'} ch={chan} {track}s"
        if start not in lines:
            out.append(f"{where}: no {start!r} line")
    else:
        shown = hcr_payload(lines, port, fn, chan, track)
        if shown is None or shown.encode() != want:
            out.append(f"{where}: the dispatch trace shows {shown!r}, expected {want!r}")
    return out


@test("ncdev.hcr_local_payload", "An HCR on NaviCore's own S4 gets, for every HCR action, the bytes W2's HCR gets for the "
      "same action (one HcrCodec on both boards); emotion 4 is refused, not an Overload; a timed fade ramps locally "
      "from its start line, and a save that moves the HCR to a WCB mid-fade stops the ramp at once, with no StopWAV "
      "(opt-in; the bytes from a NAVICORE_HIL_HOOKS image)", needs=["navicore"], links=[], opt_in="navicore_aux_tx")
def hcr_local_payload(bench):
    """executeHcrAction's local branch (NaviCore.ino:1656-1725) runs the same hcrNormalizeAction as the WCB branch and
    prints what HcrCodec::format makes (hcrFormatCommand, :1486-1488) out hcrDest's port through its tap
    (HIL_TAP(hcrSerial)->print, :1724); a per-channel VOLUP/VOLDN is made from the codec's shadow as a SetVolume
    (:1705-1714), and a fade runs the shared HcrFade from loop() (:1670-1688, :5536-5552). The table is
    ncdev.hcr_remote_verbs': the same actions must give the same bytes as W2's HCR (the WcbCmd dual compile, WCB
    CLAUDE.md rule 1), from the same starting volume. Emotion 4 is refused as on the WCB (WcbCmd 0.9 dropped the
    Overload shortcut; the comments at :1435-1447 and :1500 still describe it). The timed part: FadeIn A over 1 s
    anchors at 0 and ramps to A's level in 150 ms steps (HcrFade::start/tick, WcbHcrFade.cpp:15-67); FadeOut A over 3 s
    is cut by a SET_CONFIG that moves hcrDest back to its WCB: loop() cancels a local fade once the transport is not
    local (:5548-5552), in the pass that ran the save, so no [WIRE] S4 line follows the save's ACK and the fade-out's
    StopWAV and restore never go out (pinned: the local HCR is left at the ramp's level). Bytes go out NaviCore's own S4
    (J5), where nothing is recorded as attached: opt-in navicore_aux_tx. hcrDest is put back by nc_guard."""
    nc = _nc(bench)
    if not _hooks(nc):
        raise Skip(HOOK_SKIP)
    _ports_free(nc.config(), "S4")
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        nc.set_config({"hcrDest": {"transport": "serial", "port": "S4"}})
        with nc.debug(DBG_HCR | DBG_WIRE):
            for fn, chan, track, _, want in HCR_CASES:
                problems += _local_hcr_step(nc, fn, chan, track, want)
            for fn, chan, track, kind in HCR_REFUSED:
                nm = nc.dev.mark()
                nc.test_action(hcr(fn, chan, track))
                lines = _trace(nc, nm, 0.3)
                if hcr_refusal("Serial", fn, chan, track, kind) not in lines:
                    problems.append(f"local hcr {fn}/{chan}/{track}: no {hcr_refusal('Serial', fn, chan, track, kind)!r}")
                if wire_log(lines, "S4")[0]:
                    problems.append(f"local hcr {fn}/{chan}/{track} was refused and still wrote "
                                    f"{wire_log(lines, 'S4')[0]!r}")
            nm = nc.dev.mark()
            nc.test_action(hcr(12, 1, 1))
            time.sleep(1.6)
            lines = _trace(nc, nm, 0.1)
            ramp, gaps = wire_log(lines, "S4")
            vals = [int(v) for v in re.findall(rb"<PVA(\d+)>\n", ramp)]
            notes.append(f"FadeIn A 1 s: {vals}")
            if "[DISPATCH] HCR→S4  FadeIn ch=1 1s" not in lines:
                problems.append("FadeIn A: no '[DISPATCH] HCR→S4  FadeIn ch=1 1s' line")
            if gaps or not vals or vals[0] != 0 or vals[-1] != 75 or vals != sorted(vals) or not 4 <= len(vals) <= 12:
                problems.append(f"FadeIn A over 1 s wrote {ramp!r}{' (lines lost)' if gaps else ''}: expected <PVA0>, "
                                f"a rising ramp in 150 ms steps, then <PVA75>")
            nm = nc.dev.mark()
            nc.test_action(hcr(15, 1, 3))
            time.sleep(0.8)
            nc.set_config({"hcrDest": g.before.get("hcrDest")})
            time.sleep(1.0)
            lines = _trace(nc, nm, 0.1)
            cut = next((i for i, x in enumerate(lines) if x.startswith('{"type":"ACK","of":"SET_CONFIG"')), len(lines))
            before, after = wire_log(lines[:cut], "S4")[0], wire_log(lines[cut:], "S4")[0]
            notes.append(f"FadeOut A 3 s, cut at 0.8 s: {before!r}")
            if len(re.findall(rb"<PVA\d+>\n", before)) < 2:
                problems.append(f"FadeOut A over 3 s had not ramped before the save: {before!r}")
            if after:
                problems.append(f"the fade kept writing S4 after the save moved the HCR to a WCB: {after!r}")
            if b"<PSA" in before + after:
                problems.append("the cut fade-out still sent its StopWAV")
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncdev.local_device_bytes", "An MP3 Trigger, a DFPlayer and a WLED on NaviCore's own S4 get, for every action, the "
      "bytes the same verb gives on a WCB (one Mp3Codec, DfPlayerCodec and WcbWled on both boards): 'v' <volume> before "
      "each play, 5-step VOLUP/VOLDN, 10-byte DFPlayer frames (DEVICE 2 included), WLED JSON and a newline, a bare ;L "
      "on the lowest local id; out-of-range ones are refused (opt-in; the bytes from a NAVICORE_HIL_HOOKS image)",
      needs=["navicore"], links=[], opt_in="navicore_aux_tx")
def local_device_bytes(bench):
    """The local branches format the same ;A / ;D command as the WCB branches (mp3FormatCommand, dfpFormatCommand) and
    hand it to the WcbCmd codec bound to the port's tap - g_mp3.handle and g_dfp.handle (NaviCore.ino:1794-1806,
    :1918-1929) - and a local WLED slot gives its verb body to WcbWled::emit (:2009). So the tables are
    ncdev.mp3_remote's and ncdev.dfp_remote's and s09's WLED table, and a difference from those is a transport
    divergence (WCB CLAUDE.md rule 1), not a codec one. The traces: '[DISPATCH] MP3→S4  fn= arg= vol=  OK' with the
    shadow after the action, 'DFP→S4 ... vol=', 'WLED <id>→S4  <body>  OK'; refusals 'MP3-local: bad/out-of-range ...',
    'DFP-local: bad/out-of-range ...'. Each shadow is set by the first action (VOL 25), since NaviCore's own shadows
    live in RAM from boot (20) and whatever ran since. One device at a time on S4, each routed there inside nc_guard (a
    free WLED slot and an unused id 1-9 for the WLED), and back before the next. Device-protocol bytes out NaviCore's
    own S4 (J5), where nothing is recorded as attached: opt-in navicore_aux_tx."""
    nc = _nc(bench)
    if not _hooks(nc):
        raise Skip(HOOK_SKIP)
    cfg = nc.config()
    _ports_free(cfg, "S4")
    slots = [dict(w) for w in cfg.get("wledSlots") or []]
    free_i = next((i for i, w in enumerate(slots) if not w.get("configured")), None)
    ids = {w.get("id") for w in slots if w.get("configured")}
    wid = next((i for i in range(9, 0, -1) if i not in ids), None)
    if free_i is None or wid is None:
        raise Skip("NaviCore has no free WLED slot or id to borrow")
    others_local = any(w.get("configured") and w.get("wcb") == 0 and w.get("port") in (3, 4, 5) for w in slots)
    problems = []

    def step(action, want, trace, where):
        nm = nc.dev.mark()
        nc.test_action(action)
        lines = _trace(nc, nm, 0.3)
        problems.extend(_wire_problems(where, lines, "S4", want))
        if trace not in lines:
            problems.append(f"{where}: no {trace!r} line")
    with nc_guard(bench) as g:
        nc = g.nc
        nc.set_config({"mp3Dest": {"transport": "serial", "port": "S4"}})
        with nc.debug(DBG_MP3 | DBG_WIRE):
            for fn, track, _, hexb, vol in MP3_CASES:
                step({"type": "mp3", "fn": fn, "track": track}, bytes.fromhex(hexb),
                     f"[DISPATCH] MP3→S4  fn={fn} arg={track} vol={vol}  OK", f"local mp3 {fn}/{track}")
            for fn, track in MP3_REFUSED:
                step({"type": "mp3", "fn": fn, "track": track}, b"",
                     f"[DISPATCH] MP3-local: bad/out-of-range fn={fn} arg={track} — skipped", f"local mp3 {fn}/{track}")
        nc.set_config({"mp3Dest": g.before.get("mp3Dest"), "dfpDest": {"transport": "serial", "port": "S4"}})
        with nc.debug(DBG_DFP | DBG_WIRE):
            for (fn, chan, track, _, want), vol in zip(DFP_CASES, DFP_VOLS):
                step({"type": "dfplayer", "fn": fn, "chan": chan, "track": track}, want,
                     f"[DISPATCH] DFP→S4  fn={fn} chan={chan} track={track} vol={vol}  OK", f"local dfplayer {fn}/{chan}")
            for fn, chan, track in DFP_REFUSED:
                step({"type": "dfplayer", "fn": fn, "chan": chan, "track": track}, b"",
                     f"[DISPATCH] DFP-local: bad/out-of-range fn={fn} chan={chan} track={track} — skipped",
                     f"local dfplayer {fn}/{chan}/{track}")
        slots[free_i] = {"id": wid, "port": 4, "wcb": 0, "configured": True}
        nc.set_config({"dfpDest": g.before.get("dfpDest"), "wledSlots": slots})
        with nc.debug(DBG_WLED | DBG_WIRE):
            for verb, want in WLED_VERBS:
                step({"type": "wled", "cmd": f";L{wid},{verb}"}, want, f"[DISPATCH] WLED {wid}→S4  {verb}  OK",
                     f"local ;L{wid},{verb}")
            step({"type": "wled", "cmd": f";L{wid},BOGUS"}, b"", f"[DISPATCH] WLED {wid}→S4  BOGUS  no-op",
                 f"local ;L{wid},BOGUS")
            if not others_local:
                step({"type": "wled", "cmd": ";L,ON"}, b'{"on":true}\n', f"[DISPATCH] WLED {wid}→S4  ON  OK", "local ;L,ON")
    assert not problems, "; ".join(problems)


@test("ncdev.cli_hcr_test_codes", "#L20 and #L21 write SetEmotion(HAPPY,80) twice - the formatted frame and the raw one, "
      "<OH80,QEH> and a newline each - straight to S3 and S4, whatever hcrDest says, with their two console lines "
      "(opt-in; the bytes from a NAVICORE_HIL_HOOKS image)", needs=["navicore"], links=[], opt_in="navicore_aux_tx")
def cli_hcr_test_codes(bench):
    """#L20/#L21 (NaviCore.ino:3674-3697) bypass hcrDest and the mappings: hcrFormatCommand(2, 0, 80) and the literal
    '<OH80,QEH>\\n', both to s3 (#L20) or s4 (#L21) through the tap, between '[HCR TEST] -> <port> : SetEmotion(HAPPY,80)
    via hcrFormatCommand + raw frame' and '[HCR TEST] sent — ...'. The comment above them names GPIO15/GPIO17, a board
    profile's pins that are not the v2 ones (S3 TX GPIO8, S4 TX GPIO10, :183-186): doc drift, noted. Skips while a
    device is routed to S3 or S4. HCR frames out NaviCore's own S3/S4 (J4/J5): opt-in navicore_aux_tx; the console lines
    are checked on any image."""
    nc = _nc(bench)
    _ports_free(nc.config(), "S3", "S4")
    hook = _hooks(nc)
    problems = []
    with nc.debug(DBG_WIRE if hook else 0):
        for code, port in ((20, "S3"), (21, "S4")):
            lines = [x.rstrip() for x in nc.cli(f"#L{code}", until=r"^\[HCR TEST\] sent")]
            for want in (f"[HCR TEST] -> {port} : SetEmotion(HAPPY,80) via hcrFormatCommand + raw frame",
                         "[HCR TEST] sent — watch the HCR; check TX wiring to HCR RX, common ground"):
                if want not in lines:
                    problems.append(f"#L{code}: no {want!r}")
            if hook:
                problems += _wire_problems(f"#L{code}", lines, port, b"<OH80,QEH>\n" * 2)
    assert not problems, "; ".join(problems)


def _local_payload(nc, fn, chan, track, port="S4"):
    nm = nc.dev.mark()
    nc.test_action(hcr(fn, chan, track))
    return hcr_payload(_trace(nc, nm, 0.3), port, fn, chan, track)


@test("ncdev.hcr_local_volstep_cap", "(should) A per-channel HCR Volume Up on NaviCore's own port reaches 100, as a WCB's "
      ";H,VOLUP,<ch> and NaviCore's own all-channel step do: from 100 it must not turn the channel down to 99 (opt-in)",
      needs=["navicore"], links=[], opt_in="navicore_aux_tx")
def hcr_local_volstep_cap(bench):
    """NAVICORE.md D-NC57. For VOLUP/VOLDN on one channel the local branch steps the codec's shadow itself and clamps to
    0-99 (NaviCore.ino:1705-1714, the 99 at :1712), while HcrCodec's all-channel step and SetVolume take 0-100
    (WcbHcr.cpp:47, :113; the 99 cap was WCB issue #16, fixed in WcbCmd 0.9.0) and so does the WCB's ;H,VOLUP,<ch>
    (WCB_HCR.cpp:609-632, hcrSetVol's constrain 0-100). So the same action gives <PVA99> locally and <PVA100> through a
    WCB, and a Volume Up on a channel at 100 turns it down. Recommendation: clamp at 100. Read from the local dispatch
    trace (no hook build needed): SetVolume A 100, then Volume Up A by 5 must send <PVA100>. The control: the all-channel
    Volume Up from 97 sends 100 on each. HCR frames out NaviCore's own S4: opt-in navicore_aux_tx."""
    nc = _nc(bench)
    _ports_free(nc.config(), "S4")
    with nc_guard(bench) as g:
        nc = g.nc
        nc.set_config({"hcrDest": {"transport": "serial", "port": "S4"}})
        with nc.debug(DBG_HCR):
            control = [_local_payload(nc, 17, 3, 97), _local_payload(nc, 18, 0, 5)]
            one = [_local_payload(nc, 17, 1, 100), _local_payload(nc, 18, 2, 5)]
    bench.note(f"all channels 97 then +5: {control}; A 100 then +5: {one}")
    if control[1] != "<PVV100>\n<PVA100>\n<PVB100>\n":
        raise AssertionError(f"the control failed: the all-channel Volume Up from 97 sent {control[1]!r}")
    assert one[1] == "<PVA100>\n", f"(should, D-NC57) Volume Up on A at 100 sent {one[1]!r}: the per-channel step " \
                                   f"caps at 99"


@test("ncdev.hcr_level_same_both_ways", "(should) An HCR Trigger/Stimulate action with a level other than 0 or 1 (a "
      "legacy or hand-edited config; the tool shows it as Strong) sends the HCR the same bytes on NaviCore's own port "
      "as through a WCB: today <SH50,QEH,QT> locally and <SH1,QEH,QT> through W2 (opt-in)", needs=["navicore", "wcb1"],
      links=["W2S4"], opt_in="navicore_aux_tx")
def hcr_level_same_both_ways(bench):
    """NAVICORE.md D-NC59. HcrCodec takes a Trigger/Stimulate level of 0-99 and formats the number
    (WcbHcr.cpp:11-15, :65-68; its golden vector pins <SM30,QEM,QT>), which is what NaviCore's local branch sends. Its
    WCB branch instead maps any level of 1 or more to STRONG (hcrFormatWcbCommand, NaviCore.ino:1516-1517), which the
    WCB sends as level 1. The config tool offers only Moderate (0) and Strong (1) and shows a legacy value of 2 or more as
    Strong (config_tool/index.html:12640-12643), so a saved level 50 means one command on a local HCR and another on a
    WCB's. Recommendation: send one level both ways - normalise to 0/1 before either transport, as the tool reads it, or
    use the numeric ;H,FN form over the mesh. The remote leg uses s15's HCR on W2 S4; the local leg the dispatch trace
    with hcrDest on NaviCore's S4 (no hook build needed). The controls, levels 0 and 1, must match on both."""
    s4 = link(bench, 2, "S4")
    controls, cases = ((4, 0, 1), (3, 2, 0)), ((4, 0, 50), (3, 2, 2))
    remote, local = {}, {}
    _ports_free(_nc(bench).config(), "S4")
    with nc_guard(bench) as g:
        nc = g.nc
        _set_dest(g, "hcrDest", {"transport": "wcb", "target": "2", "wcbPort": 1})
        with _hcr_w2(bench), nc.debug(DBG_HCR):
            for key in controls + cases:
                m = s4.mark()
                nc.test_action(hcr(*key))
                time.sleep(0.8)
                remote[key] = s4.received(m)
        nc.set_config({"hcrDest": {"transport": "serial", "port": "S4"}})
        with nc.debug(DBG_HCR):
            for key in controls + cases:
                local[key] = (_local_payload(nc, *key) or "").encode()
    bench.note(f"remote {remote}; local {local}")
    off = [f"{k}: local {local[k]!r}, remote {remote[k]!r}" for k in controls if local[k] != remote[k] or not local[k]]
    if off:
        raise AssertionError("the controls differ: " + "; ".join(off))
    differ = [f"fn {k[0]} emotion {k[1]} level {k[2]}: local {local[k]!r}, through W2 {remote[k]!r}" for k in cases
              if local[k] != remote[k]]
    assert not differ, "(should, D-NC59) the same HCR action gives different bytes by transport: " + "; ".join(differ)


# ============================================================ serial actions and the mesh-to-serial bridge
@test("ncdev.serial_action_bytes", "A serial action writes its text and a CR out S3, S4 or S5 byte-exact (the hook "
      "image's DBG_WIRE copy), cut to 95 characters; a port NaviCore does not have gets nothing (NAVICORE_HIL_HOOKS "
      "build only)", needs=["navicore"], links=[])
def serial_action_bytes(bench):
    """rcExecuteActionNow's RA_SERIAL (NaviCore.ino:2093-2100) writes the cmd and '\\r' with writeS3/S4/S5 (:1400-1402),
    two blocks through the tap; the trace line names the port's label (auxPortLabel, :274-279). actionFromJson keeps 95
    characters of a cmd (RcAction.cmd[96], rc_config.h:130). A port other than S3-S5 writes nothing (its trace line is
    D-NC45's). The other aux ports must stay silent; the Maestro buses are not checked, since an easing repeat after a
    config save (the previous test's nc_guard restore) or a knob can write them at any time. Plain text out NaviCore's
    own ports, as navicore.serial_route_dbg sends on every run; skips while a device is routed to one of them. The long
    line goes to S3, the hardware UART, which buffers it: a long line to a bit-banged S4/S5 blocks loop() for its whole
    length, ncdev.serial_action_paced's finding."""
    nc = _nc(bench)
    if not _hooks(nc):
        raise Skip(HOOK_SKIP)
    cfg = nc.config()
    _ports_free(cfg, "S3", "S4", "S5")
    tag = nonce()
    problems = []

    def fire(port, cmd):
        nm = nc.dev.mark()
        nc.test_action({"type": "serial", "port": port, "cmd": cmd})
        return _trace(nc, nm, 0.3)
    with nc.debug(DBG_SERIAL | DBG_WIRE):
        for port in ("S3", "S4", "S5"):
            text = f"HILV{tag}{port}"
            lines = fire(port, text)
            problems += _wire_problems(f"serial to {port}", lines, port, text.encode() + b"\r")
            stray = [p for p in ("S3", "S4", "S5") if p != port and wire_log(lines, p)[0]]
            if stray:
                problems.append(f"serial to {port} also wrote {stray}")
            if f"[DISPATCH] Serial TX [{aux_label(cfg, port)}]  {text}" not in lines:
                problems.append(f"serial to {port}: no '[DISPATCH] Serial TX [{aux_label(cfg, port)}]  {text}'")
        lines = fire("S9", f"HILV{tag}S9")
        wrote = [p for p in ("S3", "S4", "S5") if wire_log(lines, p)[0]]
        if wrote:
            problems.append(f"serial to S9 wrote to {wrote}")
        long_cmd = f"HILL{tag}" + "X" * 100
        lines = fire("S3", long_cmd)
        problems += _wire_problems("a 106-character serial action", lines, "S3",
                                   long_cmd[:SERIAL_CMD_KEEP].encode() + b"\r")
    assert not problems, "; ".join(problems)


@test("ncdev.serial_action_paced", "(should) A serial action to a bit-banged port does not hold loop() for the whole "
      "line: a 95-character write to S4 at 9600 baud (~100 ms on the wire, past the ~96 ms of SBUS the UART can buffer) "
      "is answered as fast as a 1-character one, as the paced mesh-to-serial path to the same port is (the engine is "
      "made inert for it)", needs=["navicore"], links=[])
def serial_action_paced(bench):
    """NAVICORE.md D-NC58. A serial action writes its whole line with writeS4 (NaviCore.ino:1401, :2098), and S4/S5 are
    bit-banged SoftwareSerial whose write() returns when the last bit is out, so loop() stops for the line's length:
    ~100 ms for 95 characters and a CR at 9600. The mesh-to-serial path to the same ports hands them a few bytes a pass
    for exactly this reason - 'Writing a whole command in one go is NOT an option here ... A 200-char command at 9600
    baud is ~208 ms of that - over twenty missed SBUS frames' (auxTxPump, :5026-5086) - and Serial1 buffers ~96 ms of
    SBUS-24 (its 256-byte RX ring and the FIFO, HIL_TESTING.md §6), so the longest serial action overflows it: the stall
    sbus.stall_no_phantom measures. A USB TEST_ACTION answers after its action has run (:4017-4027), so the answer's
    delay is the time loop() was held: 5 of each, interleaved, compared by median, the difference must stay under 50
    ms. Recommendation: send RA_SERIAL through the paced auxTx queue. #L09's frame counter is read before and after
    and noted (the plan's 'fps dip'). Every stall past ~96 ms could make the reader decode a garbage frame, so the
    engine is inert meanwhile (s41 _engine_inert, in nc_guard). Text out NaviCore's own S4, as
    navicore.serial_route_dbg sends; skips while a device is routed there."""
    nc = _nc(bench)
    cfg = nc.config()
    _ports_free(cfg, "S4")
    baud = int((cfg.get("auxBaud") or {}).get("S4") or 9600)
    line_ms = (SERIAL_CMD_KEEP + 1) * 10 * 1000 / baud
    if line_ms < 60:
        raise Skip(f"S4 runs at {baud} baud: a 95-character line takes {line_ms:.0f} ms, too short to tell apart")
    tag = nonce()
    short = {"type": "serial", "port": "S4", "cmd": "H"}
    long_ = {"type": "serial", "port": "S4", "cmd": (f"HILP{tag}" + "Y" * 100)[:SERIAL_CMD_KEEP]}
    with nc_guard(bench) as g:
        nc = g.nc
        off = _engine_inert(nc, g.before)
        time.sleep(1.5)
        f0, t0 = _sbus_frames(nc)
        _ack_latency(nc, short)
        s, l = [], []
        for _ in range(5):
            s.append(_ack_latency(nc, short))
            time.sleep(0.3)
            l.append(_ack_latency(nc, long_))
            time.sleep(0.3)
        f1, t1 = _sbus_frames(nc)
    d = statistics.median(l) - statistics.median(s)
    bench.note(f"S4 at {baud} baud (a 95-character line is {line_ms:.0f} ms): TEST_ACTION answered in "
               f"{[round(x * 1000) for x in s]} ms for 1 character, {[round(x * 1000) for x in l]} ms for 95; SBUS "
               f"frames {f1 - f0} in {t1 - t0:.1f} s (knobs off meanwhile: {off})")
    assert d < PACED_MAX_S, (f"(should, D-NC58) a 95-character serial action to S4 held loop() for its line: "
                             f"TEST_ACTION answered "
                             f"{d * 1000:.0f} ms later than for 1 character (the line is {line_ms:.0f} ms at {baud} "
                             f"baud; the SBUS UART buffers about {SBUS_BUFFER_MS} ms)")


@test("ncdev.mesh_forward_burst", "Mesh-to-serial forwards that land while loop() is busy wait in a 4-deep queue: of 6 "
      ";W20,;s2 lines sent during a 3 s stall exactly the first 4 go out NaviCore's S4, each once and in order, the rest "
      "are dropped with no line, and the next one after the burst goes out (NAVICORE_HIL_HOOKS build only)",
      needs=["navicore", "wcb1"], links=[])
def mesh_forward_burst(bench):
    """onWCBCommand queues a targeted ';s<n><text>' for loop() (queueSerialFwd, NaviCore.ino:2937-2943, :3081-3096:
    mesh port 2 is firmware S4) with no wait into a 4-item queue (:4845), from the WiFi task, which keeps receiving
    while loop() is stalled; drainSerialFwd moves the head to the port's TX slot when it is free and auxTxPump clocks it
    out a few bytes a pass, logging '[DISPATCH] Serial TX [<label>]  <text>' when it is done (:5062-5105). So a burst
    that lands during a stall keeps its first four, in order, and loses the rest without a word (the queue's comment:
    'drop under load'). The stall is INF9's #L90; it outlasts the SBUS UART's buffer, so the engine is inert for it
    (s41 _engine_inert, in nc_guard), as in ncengine.mesh_trigger_burst. The burst must leave W1 well inside the stall.
    Plain text out NaviCore's own S4 (J5), as navicore.serial_route_dbg sends; skips while a device is routed there."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    if not _hooks(nc):
        raise Skip(HOOK_SKIP)
    cfg = nc.config()
    _ports_free(cfg, "S4")
    nid = nc.wcb_status()["self"]
    label, tag = aux_label(cfg, "S4"), nonce()
    texts = [f"HILF{tag}{i}" for i in range(1, 7)]
    after = f"HILF{tag}A"
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        off = _engine_inert(nc, g.before)
        time.sleep(1.5)
        with nc.debug(DBG_SERIAL | DBG_WIRE):
            nm = nc.dev.mark()
            t0 = time.monotonic()
            nc.dev.send(f"#L90,{FWD_STALL_MS}")
            time.sleep(0.15)
            for t in texts:
                w1.send(f";W{nid},;s2{t}")
            sent = time.monotonic() - t0
            nc.dev.expect(r"\[HIL\] #L90: loop\(\) resumed after \d+ ms", timeout=FWD_STALL_MS / 1000 + 5, since=nm)
            lines = _trace(nc, nm, 1.5)
            if sent > FWD_STALL_MS / 1000 - 1.0:
                raise Skip(f"W1 took {sent:.1f} s to send the burst: it may not all have landed inside the stall")
            done = [x.split("  ", 1)[1] for x in lines if x.startswith(f"[DISPATCH] Serial TX [{label}]  HILF{tag}")]
            bench.note(f"6 forwards sent {sent:.2f} s into a {FWD_STALL_MS} ms stall (knobs off: {off}); written: {done}")
            if done != texts[:FWD_QUEUE]:
                problems.append(f"after the stall S4 wrote {done}, expected the first {FWD_QUEUE}: {texts[:FWD_QUEUE]}")
            problems += _wire_problems("the burst", lines, "S4",
                                       b"".join(t.encode() + b"\r" for t in texts[:FWD_QUEUE]))
            nm = nc.dev.mark()
            w1.send(f";W{nid},;s2{after}")
            lines = _trace(nc, nm, 1.0)
            if f"[DISPATCH] Serial TX [{label}]  {after}" not in lines:
                problems.append("a forward sent after the burst was not written: the queue did not recover")
    assert not problems, "; ".join(problems)


@test("ncdev.bcast_out_opt_in", "serialBcast out on S4 (guarded): a plain mesh broadcast and an unprefixed unicast each "
      "go out NaviCore's S4 once; JSON never does, declined or not; a targeted ;s2 still goes once; with the flag back "
      "off a broadcast goes nowhere; a port a device owns is skipped (that part with navicore_aux_tx)",
      needs=["navicore", "wcb1"], links=[])
def bcast_out_opt_in(bench):
    """onWCBCommand hands an unprefixed command - a mesh broadcast, or a unicast with no ';' - to
    queueSerialBroadcastOut (NaviCore.ino:3100-3117), which queues it for every port with serialBcast out, skipping the
    port it came in on and a port a device owns (auxPortHasDevice :2965-2968: an HCR, MP3 Trigger, DFPlayer or WLED
    routed there); JSON is excluded with ';' because what rcTelemetry declines all starts with '{' (:3103-3111). A
    targeted ';s2' is written whatever the flags (:3081-3096). Written through the paced queue, each logs '[DISPATCH]
    Serial TX [<label>]  <text>' when done. The flag is set inside nc_guard, which puts it back; the device-owned part
    routes an MP3 Trigger to S4 (inside the same guard), which sends no MP3 bytes by itself but is still a device
    route: it runs only with navicore_aux_tx. The broadcast also goes out every W1 and W2 port with broadcast output
    on, probe wires all (navicore.plain_broadcast_both_ways does the same). Plain text out NaviCore's own S4; skips while
    a device is routed there."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    cfg = nc.config()
    _ports_free(cfg, "S4")
    nid = nc.wcb_status()["self"]
    label, tag, hook = aux_label(cfg, "S4"), nonce(), _hooks(nc)
    problems, head = [], f"[DISPATCH] Serial TX [{label}]  "

    def sent(text, send, wait=1.5):
        nm = nc.dev.mark()
        send()
        time.sleep(wait)
        lines = _trace(nc, nm, 0.1)
        return [x for x in lines if x == head + text], lines
    with nc_guard(bench) as g:
        nc = g.nc
        nc.set_config({"serialBcast": {"S4": {"out": True}}})
        with nc.debug(DBG_SERIAL | (DBG_WIRE if hook else 0)):
            for text, how in ((f"HILO{tag}a", lambda t: w1.send(t)), (f"HILO{tag}b", lambda t: w1.send(f";W{nid},{t}")),
                              (f"HILO{tag}d", lambda t: w1.send(f";W{nid},;s2{t}"))):
                hits, lines = sent(text, lambda: how(text))
                if len(hits) != 1:
                    problems.append(f"{text}: written out S4 {len(hits)} times, expected once")
                if hook:
                    problems += _wire_problems(text, lines, "S4", text.encode() + b"\r")
            js = f'{{"type":"HILO{tag}c"}}'
            _, lines = sent(js, lambda: w1.send(f";W{nid},{js}"))
            if any(x.startswith(head) for x in lines):
                problems.append(f"declined JSON {js} was written out S4")
            if _opted(bench, "navicore_aux_tx"):
                nc.set_config({"mp3Dest": {"transport": "serial", "port": "S4"}})
                hits, _ = sent(f"HILO{tag}e", lambda: w1.send(f"HILO{tag}e"))
                if hits:
                    problems.append("a broadcast was written out S4 while an MP3 Trigger owns it")
            else:
                bench.note("the device-owned port was not checked (navicore_aux_tx is off)")
    with nc.debug(DBG_SERIAL):
        hits, _ = sent(f"HILO{tag}f", lambda: w1.send(f"HILO{tag}f"))
    if hits:
        problems.append("with serialBcast out restored off, a broadcast was still written out S4")
    assert not problems, "; ".join(problems)
