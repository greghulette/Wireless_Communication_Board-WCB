"""NaviCore's recorder: takes recorded, saved, listed, renamed and deleted, replayed, and the timeline editor's upload
and download (docs/hil_plan/NAVICORE.md NC-WP12, ids ncrec.*; opt-in navicore_clip_write on every test that writes a
clip to flash).

Line numbers are the NaviCore tree the bench image is built from (branch hil-week, 6925773, navicore-hil1; NAVICORE.md
INF9): navicore_record.h for the recorder (namespace navirec); NaviCore.ino for the ?REC console verbs (:3456-3608),
the dispatch the recorder taps (rcExecuteActionNow :2047-2131) and loop()'s recorder calls (:5479-5490). A bare ':<n>'
in a docstring is a line of the file named last before it.

What the recorder is. One PSRAM buffer of 24000 events (recBegin, NaviCore.ino:4692) holds the take being recorded, the
clip loaded to play or download, or an upload in progress; its state is idle, RECORDING, REPLAYING or EDITING
(navicore_record.h:29). A take captures every action rcExecuteActionNow dispatches, TEST_ACTION's included, except the
record/play/stop controls (captureAction, navicore_record.h:258-263, called at NaviCore.ino:2052), and every knob
keyframe (NaviCore.ino:2687); the periodic mode and stats reports send directly (rc_telemetry.h:1261, :1291) and are
never captured. A clip on flash is a 16-byte header and the raw events, 140 bytes each (ClipFileHeader,
navicore_record.h:511-519; saveClip :535-553; the size assert :91), in the 12 MB clips partition (NaviCore.ino:4620). A
record or play action only asks: loop() runs it at its next pass (pollControl, navicore_record.h:1025-1067, called at
NaviCore.ino:5479).

Where the tests see it:
- Markers. A take is made of s41 _act markers: wcb_unicast actions to W1 whose ';S2HIL<tag><nonce>' W1 writes out of
  its S2, where probe1 times them on its own clock (the W1S2 wire). A replay re-dispatches them through the same path
  (recCbDispatch, NaviCore.ino:2192), so a replayed marker lands on the same wire, timed the same way.
- Keyframes. A clip's Maestro keyframes for remote slot 4 (device 4, which nothing hosts; s41 _remote_slot) replay as
  Pololu frames into the broadcast WCBStream, which W1's Maestro_Remote forward writes to its S1 probe (NAVICORE.md
  §1.2): nothing moves. On a NAVICORE_HIL_HOOKS image DBG_WIRE shows what NaviCore wrote (s44 wire_log).
- The console: ?REC,INFO's state and counts (info, navicore_record.h:1069-1076), ?REC,LS's [CLIPITEM] lines
  (listClips :960-982), the save, stop and cap lines, and the downloads: hil/navicore.py rec_download for a saved clip,
  and for a take that was never saved the bare ?REC,EDITLOAD, which streams the buffer in the legacy form (editStream
  :755-756, :843, :878) when no named clip is resident (NaviCore.ino:3550: a take's _loadedName is empty, cleared by
  drain(), navicore_record.h:283).

Rules every test here keeps (NAVICORE.md NC-WP12; docs/HIL_WEEK_DECISIONS.md D33):
- Clips. NaviCore's flash holds Greg's own clips. A test creates, renames and deletes only clips it named itself,
  HIL<tag><nonce> (RecGuard.name), and removes them in its guard's exit. rec_guard fails the test when a clip listed
  before it began is gone or changed (bytes, duration or events), or when a clip it never named appeared: a take saved
  with no name is auto-named rec_<N> (_autoClipName, navicore_record.h:986-994; _takeName :1005-1009). So every take
  starts with a record action carrying a HIL name (never ?REC,START, whose take has none: NaviCore.ino:3463 calls
  startRecord, which clears the name, navicore_record.h:294), and a take the test does not mean to save ends with
  ?REC,STOP, which never saves (stop() :315-328).
- The buffer. The recorder must be idle and empty when a test starts (else Skip, as s45's usb_editload_with_socket
  does: the test would replace a take someone may still want, D33), and rec_guard leaves it idle and empty whatever
  the test did: a take or a replay stopped with ?REC,STOP, an upload with ?REC,EDITCANCEL, then ?REC,CLEAR (it clears
  only when idle, clearClip :308) until ?REC,INFO shows no events, and a PING, which ends a CALIB left on
  (NaviCore.ino:3855-3858).
- Flash. Every test that writes a clip file is behind navicore_clip_write: a take saved (by the record toggle, a stop
  action, ?REC,SAVE or the 60 s backstop) or a clip uploaded (EDITEND saves, navicore_record.h:934-946). The others
  write RAM only, plus nc_guard's config writes (D-NC2).
- Servos. A replay re-sends the last target, at speed and accel 0, to every channel NaviCore has moved since boot, the
  dome's included (_buildCurveIndex, navicore_record.h:365-399; NAVICORE.md D-NC64), so every test that replays is in
  hil/servos.py.
"""
import json
import re
import time
from contextlib import contextmanager

from hil.navicore import REC_ACTION, REC_KF_HCRVOL, REC_KF_MAESTRO, NaviCore
from hil.nc_guard import nc_guard
from hil.runner import Skip, test
from suites.common import link, marker, nonce
from suites.s40_navicore_config import _flushed, _hooks, _inert_keys
from suites.s41_navicore_engine import _act, _probe_ms, _remote_slot, _wait_all
from suites.s42_navicore_sbus_engine import CMD_ACCEL, CMD_SPEED, CMD_TARGET, DBG_WIRE, PAYLOAD, _free_channels
from suites.s44_navicore_devices import wire_log

REC_MAX_MS = 60000          # the capture cap, navicore_record.h:115 (checkRecordBackstop :1012-1021)
HEADER_BYTES = 16           # ClipFileHeader, packed: magic 4, version 2, mode 2, count 4, durationMs 4 (:511-519)
EVENT_BYTES = 140           # sizeof(RecEvent) (:91): a clip file is HEADER_BYTES + n * EVENT_BYTES
EVENT_BYTES_V1 = 136        # sizeof(RecEventV1) (:86), the stride before RcAction grew skipRunning
START_WAIT_S = 2.0          # a record or play action runs at loop()'s next pass (pollControl, NaviCore.ino:5479)
MARK_GAPS_S = (0.7, 1.3)    # the spacing of a take's three markers
SPACING_MS = 50             # a marker's probe-clock spacing may differ from the clip's event times by this much
EARLY_MS, LATE_MS = 50, 150  # a replayed marker's spacing may be this much shorter / longer than the clip's
DELAY_MS = 600              # capture_scope's delayed action
SAVE_SETTLE_S = 1.5         # after a SET_CONFIG: its easing re-apply and the two repeats 500 ms apart (s44)
GATE_TAKE_S = (2.0, 2.0)    # replay_gate's take: markers at 0, +2 s, +4 s
BEFORE_CAP = 0.9            # backstop_60s sends its second marker at 90% of the cap
RAMP = ((0, 6000), (1000, 6100), (1500, 6050))   # replay_interpolation_remote's keyframes (t ms, quarter-us)
MIN_BETWEEN = 10            # distinct interpolated targets its first segment must hold, the keyframes not counted
SPAN_EARLY_MS, SPAN_LATE_MS = 60, 250   # the replayed ramp's first-to-last span against the clip's
POSE = 6400                 # replay_only_clip_channels: the pose a channel outside the clip is given
ACTION_TYPES = ("wcb_unicast", "wcb_broadcast", "maestro_local", "maestro", "serial", "hcr", "mp3", "dfplayer", "wled",
                "record", "play", "stop")   # the JSON types actionToJson writes (rc_config.h:970-1061)
SAVED = re.compile(r"^\[REC\] saved clip '(.*)'$")      # navicore_record.h:1038; ?REC,SAVE NaviCore.ino:3481
STOPPED_SAVED = re.compile(r"^\[REC\] stopped — saved clip '(.*)'$")        # a stop action, navicore_record.h:1061
PLAYBACK_DONE = r"^\[REC\] ▶ playback complete"                            # loop(), NaviCore.ino:5484
DROPPED = "[REC] recording dropped — calibration started (not saved)"       # CALIB on, NaviCore.ino:4003-4006
RENAMED, RENAME_FAILED = "[REC] renamed", "[REC] rename failed (exists / not found)"   # NaviCore.ino:3498
DELETED, DELETE_FAILED = "[REC] deleted", "[REC] delete failed"             # NaviCore.ino:3493


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


# ------------------------------------------------------------------ pure helpers (selftest.py feeds them fixtures)
def clip_bytes(n, stride=EVENT_BYTES):
    """The size ?REC,LS lists for a clip of `n` events: the header and the raw records (saveClip,
    navicore_record.h:545-549)."""
    return HEADER_BYTES + n * stride


def clip_stride(nbytes, n):
    """The record stride loadClip reads a clip of `nbytes` holding `n` events at (navicore_record.h:609-615): 140 when
    the body is n x 140, 136 when it is n x 136 (a pre-skipRunning clip, migrated in memory, :626-642), None otherwise
    (short or corrupt: loadClip then falls back to 140 and warns). n is the header's count, which LS prints."""
    body = nbytes - HEADER_BYTES
    if n <= 0 or body < 0:
        return None
    if body == n * EVENT_BYTES:
        return EVENT_BYTES
    if body == n * EVENT_BYTES_V1:
        return EVENT_BYTES_V1
    return None


def clip_changes(before, after, made=()):
    """Problems between two ?REC,LS readings, {name: (bytes, dur, n)}: a clip listed before that is gone or whose
    listing changed, a clip that appeared that is not one of `made` (the test never named it), and a clip of `made`
    still listed (not removed)."""
    out = []
    for name, stats in sorted(before.items()):
        if name not in after:
            out.append(f"clip {name} ({_stats(stats)}) is gone")
        elif after[name] != stats:
            out.append(f"clip {name} changed: {_stats(stats)} -> {_stats(after[name])}")
    for name in sorted(set(after) - set(before)):
        if name in made:
            out.append(f"clip {name}, which this test made, is still there")
        else:
            out.append(f"clip {name} ({_stats(after[name])}) appeared and this test never named it (an auto-named "
                       f"take?): left for a person to look at")
    return out


def _stats(s):
    return f"{s[0]} bytes, {s[1]} ms, {s[2]} events"


def parse_resident(lines):
    """A bare ?REC,EDITLOAD's reply -> (header, [event]): the legacy stream editStream writes for an unranged request,
    '[CLIPDL:BEGIN]{"count","durationMs","mode"}', one '[CLIPDL:EV]{...}' per event (an action: actionToJson's keys,
    then t and k; a keyframe: t, k and slot/ch/pos or chan/vol), '[CLIPDL:END]' (navicore_record.h:755-756, :841-858,
    :878). A ranged header (it has "from") answers another request and is skipped. AssertionError: a named clip is
    resident, so NaviCore looked for a clip called '' (NaviCore.ino:3550-3556); the stream was refused ([CLIPDL:ERR],
    :3576-3579); it never ended; or its events do not add up to the header's count (a line lost in NaviCore's TX
    ring)."""
    hdr, evs, ended = None, [], False
    for x in lines:
        x = x.rstrip()
        if x.startswith("[REC] clip '") and x.endswith("' not found"):
            raise AssertionError("the bare ?REC,EDITLOAD looked for a clip by name: a named clip is resident, not the "
                                 "take (NaviCore.ino:3550-3556)")
        if x.startswith("[CLIPDL:ERR]"):
            raise AssertionError(f"?REC,EDITLOAD refused: {x[len('[CLIPDL:ERR]'):][:120]}")
        if x.startswith("[CLIPDL:BEGIN]"):
            try:
                h = json.loads(x[len("[CLIPDL:BEGIN]"):])
            except ValueError:
                raise AssertionError("the [CLIPDL:BEGIN] line does not parse") from None
            if isinstance(h, dict) and "from" not in h:
                hdr, evs, ended = h, [], False
            continue
        if hdr is None or ended:
            continue
        if x.startswith("[CLIPDL:EV]"):
            try:
                evs.append(json.loads(x[len("[CLIPDL:EV]"):]))
            except ValueError:
                raise AssertionError(f"event {len(evs)} of the resident take does not parse (cut short?)") from None
        elif x == "[CLIPDL:END]":
            ended = True
    if hdr is None:
        raise AssertionError("the bare ?REC,EDITLOAD printed no [CLIPDL:BEGIN] for the resident buffer")
    if not ended:
        raise AssertionError("the bare ?REC,EDITLOAD's stream never reached [CLIPDL:END]")
    if len(evs) != hdr.get("count"):
        raise AssertionError(f"the resident buffer streamed {len(evs)} events; its header says {hdr.get('count')}")
    return hdr, evs


def all_frames(bursts):
    """Every Pololu frame in a wire's bursts [(probe ms, bytes)] -> [(ms, device, cmd, ch, value)], each stamped with
    the probe time of the burst that carried its first byte. Parsed as s42 pololu_frames parses (AA <device> <cmd>
    <payload>; a data byte is never 0xAA, so a byte that starts no known frame is skipped); ch is None for a frame with
    no payload, value None for one with no 14-bit number."""
    data, starts = b"", []
    for ms, chunk in bursts:
        starts.append((len(data), ms))
        data += chunk
    out, i = [], 0
    while i + 2 < len(data):
        if data[i] != 0xAA or data[i + 2] not in PAYLOAD:
            i += 1
            continue
        d, cmd, n = data[i + 1], data[i + 2], PAYLOAD[data[i + 2]]
        if i + 3 + n > len(data):
            break
        body = data[i + 3:i + 3 + n]
        ms = [t for off, t in starts if off <= i][-1]
        out.append((ms, d, cmd, body[0] if n else None, body[1] | (body[2] << 7) if n == 3 else None))
        i += 3 + n
    return out


def timed_frames(bursts, dev, chans):
    """The frames for Maestro `dev` on channels `chans` in a wire's bursts -> [(ms, cmd, ch, value)] (all_frames)."""
    return [(ms, c, x, v) for ms, d, c, x, v in all_frames(bursts) if d == dev and x in chans]


def _in_seg(v, prev, a, b):
    lo, hi = min(a, b), max(a, b)
    return lo <= v <= hi and (prev is None or (v - prev) * (b - a) >= 0)


def ramp_problems(values, keys, min_between=MIN_BETWEEN):
    """Problems of the setTarget values one channel got in a replay against the clip's keyframes `keys` [(t, pos)],
    time-ordered with no two adjacent positions equal; [] when the stream follows them. The replay interpolates
    linearly between the last keyframe reached and the next (_updateCurves, navicore_record.h:425-456) and sends a value
    only when it changed (:450): so the first value is the first keyframe (a channel whose pose NaviCore does not know
    snaps to it, :377, :433), every value lies on its segment and moves its way, and the last is the last keyframe, sent
    when the replay reaches it (the curve holds there, :438-439). The first segment must hold `min_between` distinct values
    strictly between its keyframes: the replay interpolated, not just stepped from keyframe to keyframe."""
    pos = [p for _, p in keys]
    if not values:
        return ["no setTarget reached the channel"]
    out = []
    if values[0] != pos[0]:
        out.append(f"the first setTarget was {values[0]}, not the first keyframe {pos[0]}")
    if values[-1] != pos[-1]:
        out.append(f"the last setTarget was {values[-1]}, not the last keyframe {pos[-1]}")
    s, prev, between = 0, None, set()
    for i, v in enumerate(values):
        while s < len(pos) - 2 and not _in_seg(v, prev, pos[s], pos[s + 1]) and _in_seg(v, prev, pos[s + 1],
                                                                                          pos[s + 2]):
            s += 1
        if len(pos) < 2 or not _in_seg(v, prev, pos[s], pos[min(s + 1, len(pos) - 1)]):
            out.append(f"setTarget #{i + 1} was {v} after {prev}: off the segment {pos[s]} -> "
                       f"{pos[min(s + 1, len(pos) - 1)]}")
            break
        if s == 0 and min(pos[0], pos[1]) < v < max(pos[0], pos[1]):
            between.add(v)
        prev = v
    if len(pos) >= 2 and len(between) < min_between:
        out.append(f"only {len(between)} distinct values between the first two keyframes ({pos[0]} -> {pos[1]}); an "
                   f"interpolated replay sends a new one whenever the position moves")
    return out


def interp_problems(frames, keys, early=SPAN_EARLY_MS, late=SPAN_LATE_MS, min_between=MIN_BETWEEN):
    """(problems, summary) of one channel's replayed frames [(ms, cmd, ch, value)] against its keyframes: speed 0 then
    accel 0 first and never again (_buildCurveIndex resets every channel the clip drives before it plays,
    navicore_record.h:395, through recCbResetChan, NaviCore.ino:2194; RECORD_REPLAY_DESIGN.md §3's precondition 1),
    then the targets (ramp_problems), whose span on the probe clock is the keyframes' within -early/+late ms."""
    first = next((i for i, f in enumerate(frames) if f[1] == CMD_TARGET), len(frames))
    pre = [(c, v) for _, c, _, v in frames[:first]]
    after = [(c, v) for _, c, _, v in frames[first:] if c != CMD_TARGET]
    targets = [(ms, v) for ms, c, _, v in frames if c == CMD_TARGET]
    out = []
    if pre != [(CMD_SPEED, 0), (CMD_ACCEL, 0)]:
        out.append(f"before its first setTarget the channel got {pre}, expected speed 0 then accel 0")
    if after:
        out.append(f"speed/accel frames came after the first setTarget: {after}")
    out += ramp_problems([v for _, v in targets], keys, min_between)
    span = targets[-1][0] - targets[0][0] if len(targets) > 1 else None
    want = keys[-1][0] - keys[0][0]
    if span is not None and not want - early <= span <= want + late:
        out.append(f"the targets spanned {span} ms on the probe clock; the keyframes span {want} ms")
    return out, f"{len(targets)} targets over {span} ms, values {targets[0][1] if targets else None}..." \
                f"{targets[-1][1] if targets else None}"


def clip_event_problems(events, n, dur):
    """(problems, notes) of a clip's downloaded events against its ?REC,LS entry (n events, `dur` ms): the count; time
    order; the last event's time equal to the listed duration (saveClip stores clipDurationMs(), the last event's tMs,
    navicore_record.h:546 and :309, and loadClip re-sorts only a buffer out of order, :569-573); every Maestro keyframe
    on slot 1-8 and channel 0-31 (_buildCurveIndex drops any other, :383); every HCR-volume keyframe on chan 0-3 (the
    knob's audio channel, NaviCore.ino:2694-2698); every action a type actionToJson names (rc_config.h:970-1061). An
    action with no type is noted, not failed: a retired type prints none (:1057-1058). A clip read at the wrong stride
    fails these: its records' kinds, times and fields come from their neighbours' bytes (the trap navicore_record.h
    :41-58 records)."""
    out, notes = [], []
    if len(events) != n:
        out.append(f"{len(events)} events downloaded; ?REC,LS lists {n}")
    ts = [e.get("t") for e in events]
    if any(not isinstance(t, int) for t in ts):
        out.append("an event has no integer time")
        return out, notes
    if ts != sorted(ts):
        out.append("the events are not in time order")
    if ts and ts[-1] != dur:
        out.append(f"the last event is at {ts[-1]} ms; ?REC,LS lists the clip's duration as {dur} ms")
    bad_kf = [i for i, e in enumerate(events) if e.get("k") == REC_KF_MAESTRO
              and not (1 <= e.get("slot", 0) <= 8 and 0 <= e.get("ch", -1) <= 31)]
    bad_vol = [i for i, e in enumerate(events) if e.get("k") == REC_KF_HCRVOL and not 0 <= e.get("chan", -1) <= 3]
    kinds = sorted({e.get("k") for e in events} - {REC_ACTION, REC_KF_MAESTRO, REC_KF_HCRVOL}, key=str)
    if bad_kf:
        out.append(f"{len(bad_kf)} Maestro keyframes outside slot 1-8 / channel 0-31 (first: event {bad_kf[0]})")
    if bad_vol:
        out.append(f"{len(bad_vol)} HCR-volume keyframes outside chan 0-3 (first: event {bad_vol[0]})")
    if kinds:
        out.append(f"events of unknown kind {kinds}")
    acts = [e for e in events if e.get("k") == REC_ACTION]
    odd = [i for i, e in enumerate(events) if e.get("k") == REC_ACTION and "type" in e and e["type"] not in ACTION_TYPES]
    untyped = sum(1 for e in acts if "type" not in e)
    if odd:
        out.append(f"{len(odd)} actions of a type actionToJson never writes (first: event {odd[0]})")
    if untyped:
        notes.append(f"{untyped} action(s) with no type (a retired action type prints none)")
    kf = sum(1 for e in events if e.get("k") == REC_KF_MAESTRO)
    notes.append(f"{len(acts)} actions, {kf} Maestro keyframes, {len(events) - len(acts) - kf} HCR-volume keyframes")
    return out, notes


# ------------------------------------------------------------------ the console
def _await_line(nc, since, pattern, timeout):
    """(match, host time) of the first NaviCore line after mark `since` that matches `pattern`, with a '#L12' poke every
    0.4 s while it waits: a lone line can sit unsent until more output follows it (HIL_TESTING.md §5). The last poke's
    Mode= reply is eaten before returning (NaviCore._eat_poke), so it cannot end a later wait. AssertionError on
    timeout, naming the pattern only."""
    rx = re.compile(pattern)
    deadline, poke, last = time.monotonic() + timeout, time.monotonic() + 0.4, None
    while True:
        for ts, x in list(nc.dev.lines[since:]):
            m = rx.search(x)
            if m:
                if last is not None:
                    nc._eat_poke(last)
                return m, ts
        now = time.monotonic()
        if now >= deadline:
            raise AssertionError(f"{nc.dev.name}: no line matching /{pattern}/ within {timeout:g} s")
        if now >= poke:
            last = nc.dev.mark()
            nc.dev.send("#L12")
            poke = now + 0.4
        time.sleep(0.02)


def _rlines(lines):
    return [x.rstrip() for x in lines]


def _ls(nc):
    """?REC,LS -> {name: (bytes, dur ms, events)}."""
    return {name: (b, d, n) for name, b, d, n in nc.rec_ls()}


def _state(nc):
    """?REC,INFO -> (state, events): 'idle', 'RECORDING', 'REPLAYING' or 'EDITING', and the buffer's event count."""
    info = nc.rec_info()
    return info[0], int(info[1])


def _await_state(nc, want, timeout=START_WAIT_S):
    """Poll ?REC,INFO until the state is `want` -> (state, events); AssertionError naming the last state otherwise."""
    deadline = time.monotonic() + timeout
    while True:
        got = _state(nc)
        if got[0] == want or time.monotonic() >= deadline:
            if got[0] != want:
                raise AssertionError(f"the recorder stayed {got[0]} ({got[1]} events); expected {want} within "
                                     f"{timeout:g} s")
            return got
        time.sleep(0.1)


def _ack_ok(ack, what):
    if not ack.get("ok"):
        raise AssertionError(f"{what}: TEST_ACTION answered {ack}")
    return ack


def _rec_start(nc, name):
    """Start a take with a record action carrying clip name `name` -> (state, events) once ?REC,INFO says RECORDING.
    A TEST_ACTION fires at once and is never gated (rcTestAction calls rcExecuteActionNow, NaviCore.ino:2155-2168);
    requestRecordToggle stores the name (navicore_record.h:332-334) and pollControl starts the take at loop()'s next
    pass, latching the name for its save (:1039-1042)."""
    _ack_ok(nc.test_action({"type": "record", "cmd": name}), f"record action {name}")
    return _await_state(nc, "RECORDING")


def _toggle_save(nc, name):
    """The second record action: while recording, the toggle stops the take and saves it under the name latched at its
    start (pollControl, navicore_record.h:1031-1038; _takeName :1005-1009) -> the name the '[REC] saved clip' line
    gives."""
    m = nc.dev.mark()
    _ack_ok(nc.test_action({"type": "record", "cmd": name}), f"record action {name}")
    return _await_line(nc, m, SAVED.pattern, 3.0)[0].group(1)


def _cli_stop(nc):
    """?REC,STOP -> its line: 'replay stopped' for a replay, else ?REC,INFO's line (NaviCore.ino:3464-3467). Never saves
    (stop(), navicore_record.h:315-328)."""
    lines = _rlines(nc.cli("?REC,STOP", until=r"^\[REC\] (replay stopped|state=)"))
    return next(x for x in lines if x.startswith(("[REC] replay stopped", "[REC] state=")))


def _cli_save(nc, name):
    """?REC,SAVE,<name> -> its outcome line (NaviCore.ino:3475-3483)."""
    lines = _rlines(nc.cli(f"?REC,SAVE,{name}", until=r"^\[REC\] (saved clip|save failed)"))
    return next(x for x in lines if x.startswith(("[REC] saved clip", "[REC] save failed")))


def _rename(nc, src, dst):
    """?REC,RENAME,<src>,<dst> -> (the '[REC] rename...' line, 'OK' or 'ERR' from [CLIPUL:RENAME,...])
    (NaviCore.ino:3494-3504)."""
    lines = _rlines(nc.cli(f"?REC,RENAME,{src},{dst}", until=r"^\[CLIPUL:RENAME,(OK|ERR)\]"))
    said = next((x for x in lines if x.startswith("[REC] rename")), None)
    tag = next(x[len("[CLIPUL:RENAME,"):-1] for x in lines if x.startswith("[CLIPUL:RENAME,"))
    return said, tag


def _rm(nc, name):
    """?REC,RM,<name> -> 'deleted' or 'delete failed' (NaviCore.ino:3493), without raising."""
    lines = _rlines(nc.cli(f"?REC,RM,{name}", until=r"^\[REC\] (deleted|delete failed)"))
    return next(x for x in lines if x in (DELETED, DELETE_FAILED))


def _clear(nc):
    """?REC,CLEAR, then ?REC,INFO -> (state, events)."""
    nc.cli("?REC,CLEAR", until=r"^\[REC\] cleared")
    return _state(nc)


def _resident(nc):
    """The buffer's events by the bare ?REC,EDITLOAD -> parse_resident's (header, events). For a take that was never
    saved: it reads RAM and writes nothing."""
    lines = _rlines(nc.cli("?REC,EDITLOAD", until=r"^\[CLIPDL:END\]|^\[CLIPDL:ERR\]|^\[REC\] clip '", timeout=10.0))
    return parse_resident(lines)


def _play(nc, name=""):
    """?REC,PLAY[,<name>] -> its line: '[REC] replaying <n> events over <ms>ms ...', '[REC] clip ... not found' or
    '[REC] busy / empty' (NaviCore.ino:3468-3474). Without a name it plays the buffer as it is."""
    lines = _rlines(nc.cli(f"?REC,PLAY,{name}" if name else "?REC,PLAY",
                           until=r"^\[REC\] (replaying|clip '|busy)"))
    return next(x for x in lines if x.startswith(("[REC] replaying", "[REC] clip '", "[REC] busy")))


def _markers_take(nc, l12, name, texts, gaps):
    """A take of `texts` as W1 S2 markers `gaps` seconds apart, started by a record action named `name`, left RECORDING
    -> the probe mark taken before it."""
    pm = l12.mark()
    _rec_start(nc, name)
    for i, t in enumerate(texts):
        if i:
            time.sleep(gaps[i - 1])
        _ack_ok(nc.test_action(_act(t)), f"marker {t}")
    got = _wait_all(l12, pm, texts, 3.0)
    lost = [t for t in texts if t.encode() + b"\r" not in got]
    if lost:
        raise AssertionError(f"markers {lost} never reached W1 S2 while recording")
    return pm


# ------------------------------------------------------------------ the recorder guard
class RecGuard:
    """One test's hold on the recorder (module docstring, Rules): enter() skips unless the recorder is idle and empty,
    and records ?REC,LS; name() hands out HIL<tag><nonce> clip names and remembers them; exit() stops whatever runs,
    empties the buffer, deletes the clips it named, and checks the listing against the one it took."""

    def __init__(self, bench, nc):
        self.bench, self.nc = bench, nc
        self.made, self.before, self.problems = [], {}, []

    def name(self, tag):
        n = f"HIL{tag}{nonce()}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", n):
            raise ValueError(f"clip name {n!r}: _clipPath keeps only [A-Za-z0-9_-] and 32 of them "
                             f"(navicore_record.h:522-533)")
        self.made.append(n)
        return n

    def enter(self):
        state, events = _state(self.nc)
        if state != "idle" or events:
            raise Skip(f"NaviCore's recorder is {state} with {events} events: this test would replace them (D33)")
        self.before = _ls(self.nc)
        return self

    def _quiesce(self):
        """Idle and empty: ?REC,EDITCANCEL for an upload, ?REC,STOP for a take or a replay (neither saves), then
        ?REC,CLEAR until ?REC,INFO shows 0 events; a PING ends a CALIB left on."""
        out = []
        for _ in range(2):
            state, events = _state(self.nc)
            if state == "EDITING":
                self.nc.cli("?REC,EDITCANCEL", until=r"^\[CLIPUL:CANCEL,")
            elif state in ("RECORDING", "REPLAYING"):
                _cli_stop(self.nc)
            state, events = _clear(self.nc)
            if state == "idle" and not events:
                break
        else:
            out.append(f"the recorder is {state} with {events} events after ?REC,STOP and ?REC,CLEAR")
        self.nc.ping()
        return out

    def exit(self, failure=None):
        problems = []
        try:
            problems += self._quiesce()
        except AssertionError as e:
            problems.append(f"the recorder could not be quieted ({str(e).splitlines()[0][:160]})")
        try:
            listed = _ls(self.nc)
            for name in self.made:
                if name in listed and _rm(self.nc, name) != DELETED:
                    problems.append(f"clip {name} could not be deleted")
            problems += clip_changes(self.before, _ls(self.nc), self.made)
        except AssertionError as e:
            problems.append(f"?REC,LS could not be read to check the clips ({str(e).splitlines()[0][:160]})")
        self.problems = problems
        if problems:
            msg = "NAVICORE CLIPS OR RECORDER NOT RESTORED — " + "; ".join(problems)
            self.bench.note(msg)
            raise AssertionError((f"{failure}\n" if failure else "") + msg)
        if failure:
            raise failure


@contextmanager
def rec_guard(bench, nc):
    """RecGuard as a block: a failure in the block is re-raised after the clean-up, with any clean-up problem after it
    (the nc_guard pattern). KeyboardInterrupt skips the clean-up."""
    g = RecGuard(bench, nc).enter()
    failure = None
    try:
        yield g
    except Exception as e:  # noqa: BLE001 - re-raised by exit() after the clean-up
        failure = e
    g.exit(failure)


def _idle_or_skip(nc):
    """Skip before anything is written (nc_guard) when the recorder holds anything (RecGuard.enter's rule)."""
    state, events = _state(nc)
    if state != "idle" or events:
        raise Skip(f"NaviCore's recorder is {state} with {events} events: this test would replace them (D33)")


# ============================================================ record, save, list, rename, delete
@test("ncrec.record_save_list_rm", "OPT-IN (navicore_clip_write): a take recorded through TEST_ACTION record - three "
      "W1 S2 markers, and the second record action stops it and saves it under the name the first carried; ?REC,LS "
      "lists it (3 events, 16 + 3 x 140 bytes, its last marker's time); the ranged download gives the three unicasts "
      "at their probe-clock spacing; ?REC,SAVE copies it; RENAME moves it, refuses an existing name and a missing "
      "clip; RM deletes it, and a second RM fails", needs=["navicore", "wcb1"], links=["W1S2"],
      opt_in="navicore_clip_write")
def record_save_list_rm(bench):
    """A record action asks for the toggle (rcExecuteActionNow's RA_RECORD, NaviCore.ino:2114-2118, calls
    requestRecordToggle, navicore_record.h:332-334), and pollControl starts the take and latches the name
    (navicore_record.h:1039-1042); the second stops it (stopRecord :302-307) and saves under that name (:1031-1038,
    '[REC] saved clip'). The record actions are never captured (:260), so the take holds the three markers alone. Each
    marker is captured at dispatch (captureAction :258-263, called at NaviCore.ino:2052) with tMs from the take's start
    (navicore_record.h:261), in the loop pass that sends its unicast, so the events' spacing is the markers'
    probe-clock spacing. ?REC,LS: bytes = 16 + n x 140 (saveClip :545-549), dur = the last event's tMs (:546, :309),
    n = the header's count (listClips :973-978). The ranged download (hil/navicore.py rec_download) loads the clip
    (loadClip :594-656), and ?REC,SAVE,<name> writes the buffer again under a new name (NaviCore.ino:3475-3483). RENAME
    refuses a destination that exists (renameClip, navicore_record.h:676) and a source that does not (the filesystem's
    rename fails, :678), each with '[REC] rename failed (exists / not found)' and [CLIPUL:RENAME,ERR]
    (NaviCore.ino:3494-3504). RM deletes (deleteClip, navicore_record.h:665-670) and a second RM fails ('[REC] delete
    failed', NaviCore.ino:3493)."""
    l12 = link(bench, 1, "S2")
    nc = _nc(bench)
    problems, notes = [], []
    with rec_guard(bench, nc) as rg:
        a, b, c, d = (rg.name(t) for t in ("Ra", "Rb", "Rc", "Rd"))
        ms = [marker(f"R{i}") for i in range(3)]
        pm = _markers_take(nc, l12, a, ms, MARK_GAPS_S)
        saved = _toggle_save(nc, a)
        info = _state(nc)
        ls1 = _ls(nc)
        evs = nc.rec_download(a)
        probe = [_probe_ms(l12, pm, m) for m in ms]
        save_b = _cli_save(nc, b)
        ls2 = _ls(nc)
        ren_ok = _rename(nc, a, c)
        ls3 = _ls(nc)
        ren_exists = _rename(nc, c, b)
        ren_missing = _rename(nc, a, d)
        ls4 = _ls(nc)
        rm1, rm2, rm3 = _rm(nc, c), _rm(nc, c), _rm(nc, b)
        ls5 = _ls(nc)
    if saved != a:
        problems.append(f"the record toggle saved clip {saved!r}, not {a!r}, the name its start carried")
    if info != ("idle", 3):
        problems.append(f"after the save ?REC,INFO shows {info}, expected idle with 3 events")
    want_cmds = [f";S2{m}" for m in ms]
    got_cmds = [e.get("cmd") for e in evs]
    if got_cmds != want_cmds or any((e.get("k"), e.get("type"), e.get("target")) != (REC_ACTION, "wcb_unicast", "1")
                                    for e in evs):
        shape = [(e.get("k"), e.get("type"), e.get("target"), e.get("cmd")) for e in evs]
        problems.append(f"the clip downloads as {shape}; expected the three unicasts to W1 {want_cmds}")
    elif ls1.get(a) != (clip_bytes(3), evs[-1]["t"], 3):
        problems.append(f"?REC,LS lists {a} as {ls1.get(a)}, expected {(clip_bytes(3), evs[-1]['t'], 3)} (bytes, "
                        f"dur, events)")
    if len(evs) == 3 and None not in probe:
        for i in (1, 2):
            dt, dp = evs[i]["t"] - evs[i - 1]["t"], probe[i] - probe[i - 1]
            notes.append(f"marker {i}: {dt} ms in the clip, {dp} ms on the probe clock")
            if abs(dt - dp) > SPACING_MS:
                problems.append(f"marker {i} is {dt} ms after the one before in the clip but {dp} ms on the probe "
                                f"clock (more than {SPACING_MS} ms apart)")
    if save_b != f"[REC] saved clip '{b}'" or ls2.get(b) != ls1.get(a):
        problems.append(f"?REC,SAVE,{b} said {save_b!r} and LS lists it as {ls2.get(b)}; expected a copy of {a} "
                        f"({ls1.get(a)})")
    if ren_ok != (RENAMED, "OK") or a in ls3 or ls3.get(c) != ls1.get(a):
        problems.append(f"RENAME {a} -> {c} answered {ren_ok}; LS then lists {a}: {ls3.get(a)}, {c}: {ls3.get(c)}")
    if ren_exists != (RENAME_FAILED, "ERR"):
        problems.append(f"RENAME onto the existing {b} answered {ren_exists}; it must refuse")
    if ren_missing != (RENAME_FAILED, "ERR"):
        problems.append(f"RENAME of the missing {a} answered {ren_missing}; it must refuse")
    if ls4.get(c) != ls1.get(a) or ls4.get(b) != ls1.get(a) or d in ls4:
        problems.append(f"after the refused RENAMEs LS lists {c}: {ls4.get(c)}, {b}: {ls4.get(b)}, {d}: {ls4.get(d)}")
    if (rm1, rm2, rm3) != (DELETED, DELETE_FAILED, DELETED):
        problems.append(f"RM {c}, RM {c} again, RM {b} answered {(rm1, rm2, rm3)}; expected deleted, delete failed, "
                        f"deleted")
    if {a, b, c, d} & set(ls5):
        problems.append(f"after the RMs LS still lists {sorted({a, b, c, d} & set(ls5))}")
    bench.note("ncrec.record_save_list_rm: " + "; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ what a take captures
@test("ncrec.capture_scope", "A take holds every dispatched action and nothing else: a TEST_ACTION marker, a TRIGGERed "
      "tier's two markers (the delayed one at the time it fired, 600 ms on, its delay kept in the record) and a "
      "Maestro setSpeed action, in order at their probe-clock spacing; not a play action sent meanwhile (a control, "
      "which also leaves the take running); ?REC,STOP ends it unsaved (RAM only: no clip is written)",
      needs=["navicore", "wcb1"], links=["W1S2"])
def capture_scope(bench):
    """captureAction (navicore_record.h:258-263) is called first thing in rcExecuteActionNow (NaviCore.ino:2052), which
    every dispatch reaches: TEST_ACTION (rcTestAction, NaviCore.ino:2155-2168), a TRIGGERed tier (rcDispatch
    :2281-2306, then rcExecuteAction :2135-2147), and a delayed action when checkPendingActions fires its copy
    (:2202-2220). So the delayed marker is captured at its fire time with the pending copy's delayMs still set, which
    actionToJson writes as "delay" (rc_config.h:976); the replay ignores it (recCbDispatch calls rcExecuteActionNow,
    NaviCore.ino:2192). RECORD_REPLAY_DESIGN.md §4 says stored events carry delay 0 and §5 that setSpeed is never
    captured: doc drift (NAVICORE.md D-NC36), noted; the code is followed. A play action is never captured
    (navicore_record.h:260), and pollControl ignores it while recording (:1045-1053: neither REPLAYING nor idle).
    ?REC,STOP ends the take unsaved (stop() :315-328) and prints ?REC,INFO (NaviCore.ino:3464-3467); the bare
    ?REC,EDITLOAD then streams the take from RAM (parse_resident). The mapping sits on an inert key inside nc_guard (s40
    _inert_keys); the setSpeed goes to remote slot 4 with speed 0 on a channel no knob drives (nothing hosts device 4,
    and 0 is the speed cache's starting value, NaviCore.ino:943-949)."""
    l12 = link(bench, 1, "S2")
    problems, notes = [], []
    _idle_or_skip(_nc(bench))
    with nc_guard(bench) as g:
        nc = g.nc
        key = _inert_keys(g.before, 1)[0]
        mode, btn = divmod(int(key), 100)
        slot, dev = _remote_slot(g.before)
        (ch,) = _free_channels(g.before, slot, 1)
        a, b, c = marker("Ca"), marker("Cb"), marker("Cc")
        nc.set_config({"mappings": {key: {"exclusive": False, "t1": [_act(b), _act(c, DELAY_MS)]}}})
        time.sleep(SAVE_SETTLE_S)
        with rec_guard(bench, nc) as rg:
            name = rg.name("Ck")
            pm = _markers_take(nc, l12, name, [a], ())
            ack, trig = nc.trigger(mode, btn, 1)
            got = _wait_all(l12, pm, [a, b, c], 3.0)
            _ack_ok(nc.test_action({"type": "maestro", "target": str(slot), "cmd": f"setSpeed,{ch},0"}), "setSpeed")
            _ack_ok(nc.test_action({"type": "play", "cmd": rg.name("Cp"), "fn": 0}), "play action")
            time.sleep(0.4)
            mid = _state(nc)
            stop = _cli_stop(nc)
            hdr, evs = _resident(nc)
            probe = {t: _probe_ms(l12, pm, t) for t in (a, b, c)}
    if not ack.get("ok"):
        problems.append(f"TRIGGER {mode},{btn},1 answered {ack}")
    lost = [t for t in (a, b, c) if t.encode() + b"\r" not in got]
    if lost:
        problems.append(f"markers {lost} never reached W1 S2")
    if mid != ("RECORDING", 4):
        problems.append(f"after the play action ?REC,INFO showed {mid}; expected still RECORDING with 4 events (a play "
                        f"is ignored while recording, and never captured)")
    if not stop.startswith("[REC] state=idle  events=4/"):
        problems.append(f"?REC,STOP answered {stop!r}; expected ?REC,INFO's line, idle with 4 events")
    want = [("wcb_unicast", "1", f";S2{a}"), ("wcb_unicast", "1", f";S2{b}"), ("wcb_unicast", "1", f";S2{c}"),
            ("maestro", str(slot), f"setSpeed,{ch},0")]
    shape = [(e.get("type"), e.get("target"), e.get("cmd")) for e in evs]
    if shape != want or any(e.get("k") != REC_ACTION for e in evs):
        problems.append(f"the take holds {shape}; expected {want}, all actions (no play action, no rc_trig)")
    else:
        ta, tb, tc = (evs[i]["t"] for i in range(3))
        if evs[2].get("delay") != DELAY_MS or "delay" in evs[0] or "delay" in evs[1]:
            problems.append(f"the delays recorded are {[e.get('delay') for e in evs[:3]]}; expected none, none, "
                            f"{DELAY_MS} (the pending copy's delayMs, kept)")
        if not DELAY_MS - 2 <= tc - tb <= DELAY_MS + 60:
            problems.append(f"the delayed marker was captured {tc - tb} ms after its tier's first; it fires "
                            f"{DELAY_MS} ms on")
        if tb < ta:
            problems.append(f"the TRIGGERed marker ({tb} ms) is before the TEST_ACTION one ({ta} ms)")
        if None not in probe.values():
            for (x, y, dt) in ((a, b, tb - ta), (b, c, tc - tb)):
                dp = probe[y] - probe[x]
                notes.append(f"{dt} ms in the take, {dp} ms on the probe clock")
                if abs(dt - dp) > SPACING_MS:
                    problems.append(f"markers {x} -> {y}: {dt} ms apart in the take, {dp} ms on the probe clock")
    notes.append(f"rc_trig on USB: {'yes' if trig else 'none (USB had no room for it, NaviCore.ino:2257-2276)'}")
    bench.note(f"ncrec.capture_scope: mapping {key}, slot {slot} ch {ch}; " + "; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ stopping a take, a replay
@test("ncrec.stop_semantics", "OPT-IN (navicore_clip_write): a stop action ends a take and saves it; ?REC,STOP ends one "
      "unsaved and keeps it in RAM for ?REC,SAVE,<name>; the record toggle saves under the name its start carried, "
      "whatever name a later play or record action carried; ?REC,STOP ends a replay ('replay stopped') and the rest of "
      "the clip never arrives", needs=["navicore", "wcb1"], links=["W1S2"], opt_in="navicore_clip_write")
def stop_semantics(bench):
    """pollControl (navicore_record.h:1025-1067): CTL_STOP while recording stops and saves ('[REC] stopped — saved
    clip', :1054-1061), under _takeName, the name latched when the take started (:1005-1009, latched at :1041); while
    replaying or idle it only stops (:1063). CTL_REC_TOGGLE while recording saves under the same latched name
    (:1031-1038), so neither the play action's name (requestPlay writes _pendingName, :335-337) nor the stopping record
    action's own name can redirect the save. ?REC,STOP calls stop(), which never saves (:315-328): after a take it
    prints ?REC,INFO with the take's count (the buffer keeps it, NaviCore.ino:3464-3467), after a replay 'replay
    stopped'; ?REC,SAVE,<name> then saves the buffer (:3475-3483). The replay part plays that kind of RAM take (?REC,PLAY
    with no name plays the buffer, :3468-3474) and stops it after its first marker, 1.5 s before the second."""
    l12 = link(bench, 1, "S2")
    nc = _nc(bench)
    problems = []
    with rec_guard(bench, nc) as rg:
        # A: the stop action saves
        na = rg.name("Sa")
        _markers_take(nc, l12, na, [marker("Sa")], ())
        m = nc.dev.mark()
        _ack_ok(nc.test_action({"type": "stop"}), "stop action")
        got = _await_line(nc, m, STOPPED_SAVED.pattern, 3.0)[0].group(1)
        a_state, a_ls = _state(nc), _ls(nc).get(na)
        if got != na or a_state != ("idle", 1) or not a_ls or (a_ls[0], a_ls[2]) != (clip_bytes(1), 1):
            problems.append(f"the stop action saved {got!r} ({a_ls}), recorder {a_state}; expected {na}, 1 event, idle")
        # B: ?REC,STOP keeps the take unsaved; ?REC,SAVE saves it
        nb, nc_name = rg.name("Sb"), rg.name("Sc")
        _markers_take(nc, l12, nb, [marker("Sb")], ())
        stop = _cli_stop(nc)
        b_ls = _ls(nc)
        saved = _cli_save(nc, nc_name)
        c_ls = _ls(nc)
        if not stop.startswith("[REC] state=idle  events=1/") or nb in b_ls:
            problems.append(f"?REC,STOP after a take answered {stop!r} and LS lists {nb}: {b_ls.get(nb)}; expected "
                            f"idle with the take's 1 event, nothing saved")
        if saved != f"[REC] saved clip '{nc_name}'" or (c_ls.get(nc_name) or (0,))[0] != clip_bytes(1):
            problems.append(f"?REC,SAVE,{nc_name} said {saved!r}, LS lists {c_ls.get(nc_name)}; expected the kept take")
        # C: the toggle saves under the latched name
        nd, ne, nf = rg.name("Sd"), rg.name("Se"), rg.name("Sf")
        _markers_take(nc, l12, nd, [marker("Sd")], ())
        _ack_ok(nc.test_action({"type": "play", "cmd": ne, "fn": 0}), "play action")
        time.sleep(0.2)
        got = _toggle_save(nc, nf)
        d_ls = _ls(nc)
        if got != nd or nd not in d_ls or ne in d_ls or nf in d_ls:
            problems.append(f"the record toggle carrying {nf} (after a play of {ne}) saved {got!r}; expected {nd}, the "
                            f"name the take started with")
        _clear(nc)
        # D: ?REC,STOP ends a replay
        ng = rg.name("Sg")
        m0, m1 = marker("Sg0"), marker("Sg1")
        _markers_take(nc, l12, ng, [m0, m1], (1.5,))
        _cli_stop(nc)
        pm, nm = l12.mark(), nc.dev.mark()
        started = _play(nc)
        l12.expect(m0.encode() + b"\r", timeout=3.0, since=pm)
        stopped = _cli_stop(nc)
        time.sleep(2.0)
        tail = l12.received(pm)
        d_state = _state(nc)
        g_ls = _ls(nc)
        if not started.startswith("[REC] replaying 2 events over "):
            problems.append(f"?REC,PLAY of the kept take answered {started!r}")
        if stopped != "[REC] replay stopped" or d_state[0] != "idle":
            problems.append(f"?REC,STOP during the replay answered {stopped!r}, recorder {d_state}")
        if m1.encode() in tail:
            problems.append("the replay's second marker arrived after ?REC,STOP stopped it")
        if ng in g_ls:
            problems.append(f"stopping the replay saved {ng}")
    assert not problems, "; ".join(problems)


# ============================================================ replay timing
@test("ncrec.play_timing_markers", "OPT-IN (navicore_clip_write): a saved take replays its three markers at the spacing "
      "they were recorded with (each within -50/+150 ms on the probe clock of the clip's own event times), once each, "
      "then 'playback complete'; the play action toggles: sent again while its clip plays, it stops it and the rest "
      "never arrives", needs=["navicore", "wcb1"], links=["W1S2"], opt_in="navicore_clip_write")
def play_timing_markers(bench):
    """A play action (RA_PLAY, NaviCore.ino:2119-2122) asks for CTL_PLAY with the clip's name; pollControl loads it and
    starts the replay (navicore_record.h:1045-1052; loadClip :594-656; startReplay :459-468), and replayTick fires each
    event once its tMs has elapsed since the replay's start (:472-483), through the same dispatch the take came from
    (recCbDispatch, NaviCore.ino:2192), so the markers land on W1 S2 at the clip's spacing. When the last has fired the
    replay ends (navicore_record.h:486-493) and loop() prints '[REC] ▶ playback complete' (NaviCore.ino:5483-5484). A
    second play action while the clip plays toggles it off (navicore_record.h:1046-1047), and the events left never
    fire. The clip's times come from its ranged download; the markers are timed on the probe clock (s41 _probe_ms), so
    USB latency cancels (probe-clock delays land within 3 ms of nominal in s41's ncengine.delay_queue)."""
    l12 = link(bench, 1, "S2")
    nc = _nc(bench)
    problems, notes = [], []
    with rec_guard(bench, nc) as rg:
        name = rg.name("P")
        ms = [marker(f"P{i}") for i in range(3)]
        rec_pm = _markers_take(nc, l12, name, ms, MARK_GAPS_S)
        if _toggle_save(nc, name) != name:
            raise AssertionError(f"the take was not saved as {name}")
        evs = nc.rec_download(name)
        ts = [e["t"] for e in evs]
        rec_probe = [_probe_ms(l12, rec_pm, m) for m in ms]
        # a whole replay
        pm, nm = l12.mark(), nc.dev.mark()
        _ack_ok(nc.test_action({"type": "play", "cmd": name, "fn": 0}), "play action")
        _await_line(nc, nm, PLAYBACK_DONE, (ts[-1] - ts[0]) / 1000 + 4)
        time.sleep(0.5)
        got = l12.received(pm)
        play = [_probe_ms(l12, pm, m) for m in ms]
        after = _state(nc)
        # the toggle
        pm2 = l12.mark()
        _ack_ok(nc.test_action({"type": "play", "cmd": name, "fn": 0}), "play action")
        l12.expect(ms[0].encode() + b"\r", timeout=3.0, since=pm2)
        _ack_ok(nc.test_action({"type": "play", "cmd": name, "fn": 0}), "the second play action")
        time.sleep(0.3)
        toggled = _state(nc)
        time.sleep((ts[-1] - ts[0]) / 1000 + 0.5)
        tail = l12.received(pm2)
    counts = [got.count(m.encode() + b"\r") for m in ms]
    if counts != [1, 1, 1]:
        problems.append(f"the replay put the markers on W1 S2 {counts} times; expected once each")
    if after[0] != "idle":
        problems.append(f"after 'playback complete' the recorder is {after[0]}")
    if len(ts) != 3:
        problems.append(f"the clip downloads as {len(ts)} events, not 3")
    elif None not in play:
        for i in (1, 2):
            dt, dp = ts[i] - ts[i - 1], play[i] - play[i - 1]
            dr = rec_probe[i] - rec_probe[i - 1] if None not in rec_probe else None
            notes.append(f"gap {i}: clip {dt} ms, recorded {dr} ms, replayed {dp} ms")
            if not dt - EARLY_MS <= dp <= dt + LATE_MS:
                problems.append(f"replayed gap {i} was {dp} ms on the probe clock; the clip's is {dt} ms")
    else:
        problems.append("a replayed marker has no probe time")
    if toggled[0] != "idle":
        problems.append(f"the second play action left the recorder {toggled[0]}; a play while playing stops the clip")
    if any(m.encode() in tail for m in ms[1:]):
        problems.append("markers after the first arrived although the second play action stopped the replay")
    bench.note("ncrec.play_timing_markers: " + "; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ the replay gate
@test("ncrec.replay_gate", "While a clip plays it owns the outputs: a USB TRIGGER of a mapped tier is ACKed but fires "
      "nothing, a delayed action that comes due is dropped, TEST_ACTION still fires, a CALIB leaves the replay "
      "dispatching; a mapped stop action is let through and ends the replay; afterwards the same TRIGGER fires again "
      "(a RAM-only take: no clip is written)", needs=["navicore", "wcb1"], links=["W1S2"])
def replay_gate(bench):
    """rcExecuteAction (NaviCore.ino:2135-2147) drops every live action while navirec::isReplaying() (:2144, the
    record/play/stop controls excepted) before it schedules a delay (:2145); checkPendingActions drops a delayed copy
    that comes due during a replay, with the same exemption (:2216-2217); TEST_ACTION calls rcExecuteActionNow directly
    (:2166), past both gates. rcDispatch emits rc_trig before any action (:2235-2279) and the USB TRIGGER ACKs after it
    (:4051-4061), so a gated TRIGGER is still ok:true. The replay itself dispatches through recCbDispatch
    (rcExecuteActionNow, :2192) with no calibration gate, so a CALIB turned on mid-replay mutes live dispatch but not
    the clip (the plan's 'replay exemption'; PROTOCOLS.md says CALIB mutes all dispatch: D-NC36's doc drift). A mapped
    stop action passes the gate and CTL_STOP stops the replay without saving (navicore_record.h:1062-1063). Mappings on
    inert keys inside nc_guard; the take is RAM only (?REC,STOP, then ?REC,PLAY with no name plays the buffer,
    NaviCore.ino:3468-3474). The timeline, from the ?REC,PLAY: the delayed action was scheduled just before it and falls
    due at +1 s; the live TRIGGER at +0.3 s, the TEST_ACTION at +0.6 s; CALIB on from +1.5 s to +2.7 s, around the
    second marker (about +2.1 s); the stop TRIGGER at +3.2 s, a second before the third marker."""
    l12 = link(bench, 1, "S2")
    problems = []
    _idle_or_skip(_nc(bench))
    with nc_guard(bench) as g:
        nc = g.nc
        kg, kd, ks = _inert_keys(g.before, 3)
        gm, dm, tm = marker("Gg"), marker("Gd"), marker("Gt")
        nc.set_config({"mappings": {kg: {"exclusive": False, "t1": [_act(gm)]},
                                    kd: {"exclusive": False, "t1": [_act(dm, 1000)]},
                                    ks: {"exclusive": False, "t1": [{"type": "stop"}]}}})
        time.sleep(SAVE_SETTLE_S)

        def trig(k):
            mode, btn = divmod(int(k), 100)
            return nc.trigger(mode, btn, 1)[0]
        with rec_guard(bench, nc) as rg:
            ms = [marker(f"G{i}") for i in range(3)]
            _markers_take(nc, l12, rg.name("G"), ms, GATE_TAKE_S)
            _cli_stop(nc)
            pm = l12.mark()
            acks = {"delayed": trig(kd)}
            t0 = time.monotonic()
            started = _play(nc)
            time.sleep(max(0.0, t0 + 0.3 - time.monotonic()))
            acks["live"] = trig(kg)
            time.sleep(max(0.0, t0 + 0.6 - time.monotonic()))
            _ack_ok(nc.test_action(_act(tm)), "TEST_ACTION marker")
            time.sleep(max(0.0, t0 + GATE_TAKE_S[0] - 0.5 - time.monotonic()))
            nc.calib(True)
            under_calib = _state(nc)
            time.sleep(max(0.0, t0 + GATE_TAKE_S[0] + 0.7 - time.monotonic()))
            nc.calib(False)
            time.sleep(max(0.0, t0 + GATE_TAKE_S[0] + 1.2 - time.monotonic()))
            acks["stop"] = trig(ks)
            time.sleep(0.4)
            after_stop = _state(nc)
            time.sleep(max(0.0, t0 + sum(GATE_TAKE_S) + 0.8 - time.monotonic()))
            during = l12.received(pm)
            pm2 = l12.mark()
            acks["again"] = trig(kg)
            again = _wait_all(l12, pm2, [gm], 3.0)
    if not started.startswith("[REC] replaying 3 events over "):
        problems.append(f"?REC,PLAY answered {started!r}")
    bad = {k: a for k, a in acks.items() if not a.get("ok")}
    if bad:
        problems.append(f"TRIGGER ACKs not ok: {bad}")
    if ms[0].encode() + b"\r" not in during or ms[1].encode() + b"\r" not in during:
        problems.append("the replay's first two markers did not both arrive (the second fired under CALIB)")
    if under_calib[0] != "REPLAYING":
        problems.append(f"CALIB on left the recorder {under_calib[0]}; the replay runs on")
    if gm.encode() in during:
        problems.append("a TRIGGERed marker fired during the replay")
    if dm.encode() in during:
        problems.append("a delayed marker that came due during the replay fired")
    if tm.encode() + b"\r" not in during:
        problems.append("the TEST_ACTION marker sent during the replay never arrived")
    if after_stop[0] != "idle":
        problems.append(f"after the mapped stop action the recorder is {after_stop[0]}")
    if ms[2].encode() in during:
        problems.append("the replay's third marker arrived after the stop action")
    if gm.encode() + b"\r" not in again:
        problems.append("after the replay the same TRIGGER fired nothing")
    assert not problems, "; ".join(problems)


# ============================================================ interpolation
@test("ncrec.replay_interpolation_remote", "OPT-IN (navicore_clip_write): a clip uploaded with EDITBEGIN / indexed "
      "EDITEV / EDITEND downloads back event for event; replayed, its remote slot-4 channel gets speed 0 and accel 0 "
      "first, then a dense interpolated setTarget stream on W1 S1 that follows the keyframes (6000 -> 6100 over 1000 "
      "ms -> 6050 at 1500 ms: the first keyframe first, monotone per segment, the last keyframe last) over their span; "
      "nothing after it (remote slot: nothing moves)", needs=["navicore", "wcb1"], links=["W1S1"],
      opt_in="navicore_clip_write")
def replay_interpolation_remote(bench):
    """The upload (hil/navicore.py rec_upload): editBegin empties the buffer into ST_EDITING (navicore_record.h
    :883-888), editAddEvent writes each event at its index (:897-925; the ACK echoes the index, NaviCore.ino:3587-3598),
    and editEnd re-sorts and saves (navicore_record.h:934-946; '[CLIPUL:END,OK]', NaviCore.ino:3599-3603). The replay
    (?REC,PLAY,<name>, NaviCore.ino:3468-3474): _buildCurveIndex resets speed and accel on every channel the clip drives
    (navicore_record.h:395, through recCbResetChan, NaviCore.ino:2194) and, the channel's pose unknown (?MAE,<slot>,
    <ch>,0 first: a target of 0 invalidates the shadow, NaviCore.ino:1038-1045), sends it no anchor (navicore_record.h
    :377, :396); then every replayTick eases the channel from the keyframe last reached toward the next (_updateCurves
    :425-456: pos = pPrev + (pNext - pPrev) x frac, truncated) and sends a value only when it changed (:450), so a
    100-count segment over 1000 ms is a dense stream; the last keyframe is sent when reached, the curve holds there
    (:438-439), and the replay completes in that pass (:486-493). Keyframes are never fired as events (:479). The
    frames go into the broadcast WCBStream (maestroWrite, NaviCore.ino:647-701) and W1's Maestro_Remote forward writes
    them to its S1; the broadcast is unacknowledged, so when W1 S1 misses frames that a hook image's DBG_WIRE copy shows
    NaviCore wrote correctly, the replay is played once more before a miss counts. Other channels' frames (D-NC64's reset
    of every channel with a known pose) are left out: only this channel's are judged."""
    l11 = link(bench, 1, "S1")
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    (ch,) = _free_channels(cfg, slot, 1)
    events = [{"t": t, "k": REC_KF_MAESTRO, "slot": slot, "ch": ch, "pos": p} for t, p in RAMP]
    want_ls = (clip_bytes(len(events)), RAMP[-1][0], len(events))
    hook = _hooks(nc)
    problems, notes = [], []
    with rec_guard(bench, nc) as rg:
        name = rg.name("I")
        nc.rec_upload(name, events)
        listed = _ls(nc).get(name)
        back = nc.rec_download(name)
        if listed != want_ls:
            problems.append(f"?REC,LS lists the upload as {listed}; expected {want_ls} (bytes, dur, events)")
        if back != events:
            problems.append(f"the upload downloads as {back}; expected {events}")
        try:
            for attempt in (1, 2):
                nc.mae_set(slot, ch, 0)
                time.sleep(0.3)
                m11, nm = l11.mark(), nc.dev.mark()
                with nc.debug(DBG_WIRE if hook else 0):
                    started = _play(nc, name)
                    _await_line(nc, nm, PLAYBACK_DONE, RAMP[-1][0] / 1000 + 4)
                    time.sleep(0.6)
                    lines = _flushed(nc, nm, 0.05)
                frames = timed_frames(l11.bursts(m11), dev, {ch})
                probs, summary = interp_problems(frames, RAMP)
                wrote = None
                if hook:
                    data, gaps = wire_log(lines, "WCBStream")
                    if not gaps:
                        wrote = timed_frames([(0, data)], dev, {ch})
                notes.append(f"attempt {attempt}: {summary}")
                if not started.startswith("[REC] replaying 3 events over 1500ms"):
                    problems.append(f"?REC,PLAY,{name} answered {started!r}")
                    break
                if not probs:
                    break
                wire_ok = wrote is not None and not ramp_problems([v for _, c, _, v in wrote if c == CMD_TARGET], RAMP)
                if attempt == 1 and wire_ok:
                    notes.append("W1 S1 missed frames NaviCore's [WIRE] log shows it wrote: played once more")
                    continue
                where = "" if wrote is None else (" (NaviCore's [WIRE] WCBStream copy follows the keyframes)" if wire_ok
                                                  else " (NaviCore's [WIRE] WCBStream copy shows the same)")
                problems += [f"W1 S1: {p}{where}" for p in probs]
                break
        finally:
            nc.mae_set(slot, ch, 0)
    bench.note(f"ncrec.replay_interpolation_remote: slot {slot} (device {dev}) ch {ch}; " + "; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ CALIB and a take
@test("ncrec.calib_drops_take", "CALIB turned on during a take drops it unsaved: 'recording dropped — calibration "
      "started (not saved)' before the ACK, the recorder idle with the take's event still in RAM, no clip under the "
      "take's name; CALIB off resumes dispatch (RAM only)", needs=["navicore", "wcb1"], links=["W1S2"])
def calib_drops_take(bench):
    """The CALIB handler (NaviCore.ino:3995-4010): turning calibration on while RECORDING calls stopRecord() and prints
    the drop line, never saving (a take must not straddle a calibration, NaviCore.ino:3997-4006), then '[CALIB] action
    dispatch SUPPRESSED (calibrating)' and the ACK (:4007-4010). stopRecord flushes and goes idle and leaves the events
    in the buffer (navicore_record.h:302-307: _count untouched). CALIB off prints 'resumed' (NaviCore.ino:4008-4009)."""
    l12 = link(bench, 1, "S2")
    nc = _nc(bench)
    problems = []
    with rec_guard(bench, nc) as rg:
        name = rg.name("Q")
        _markers_take(nc, l12, name, [marker("Q")], ())
        time.sleep(0.2)
        try:
            m = nc.dev.mark()
            ack = nc.calib(True)
            lines = _rlines(nc.dev.since(m))
            state = _state(nc)
            listed = name in _ls(nc)
        finally:
            m2 = nc.dev.mark()
            off = nc.calib(False)
            off_lines = _rlines(nc.dev.since(m2))
    i_drop = lines.index(DROPPED) if DROPPED in lines else None
    i_ack = next((i for i, x in enumerate(lines) if x.startswith('{"type":"ACK"')), None)
    if i_drop is None or i_ack is None or i_drop > i_ack:
        problems.append(f"CALIB on printed {lines[:4]}; expected '{DROPPED}' before its ACK")
    if "[CALIB] action dispatch SUPPRESSED (calibrating)" not in lines or not ack.get("ok"):
        problems.append(f"CALIB on answered {ack} without the SUPPRESSED line")
    if state != ("idle", 1):
        problems.append(f"after CALIB on ?REC,INFO shows {state}; expected idle with the take's 1 event")
    if listed:
        problems.append(f"the dropped take was saved as {name}")
    if "[CALIB] action dispatch resumed" not in off_lines or not off.get("ok"):
        problems.append(f"CALIB off answered {off} without the 'resumed' line")
    assert not problems, "; ".join(problems)


# ============================================================ the 60 s backstop
@test("ncrec.backstop_60s", "OPT-IN (navicore_clip_write, ~65 s): a take left running stops itself 60 s after it "
      "started and saves under its record action's name ('capped at 60s — saved clip'); a marker sent before the cap "
      "is in it, one sent after is not", needs=["navicore", "wcb1"], links=["W1S2"], opt_in="navicore_clip_write")
def backstop_60s(bench):
    """checkRecordBackstop (navicore_record.h:1012-1021), called from loop() after drain() (NaviCore.ino:5480-5481):
    once millis() - _recStart passes REC_MAX_MS (60000, :115) it stops capturing, goes idle and saves under _takeName,
    the name the take's record action carried (:1005-1009), printing '[REC] capped at 60s — saved clip '<name>''. A
    marker dispatched after that is not captured (_capturing false, :258-259). The clip holds the two markers sent
    before the cap; its duration is the second's time (:546)."""
    l12 = link(bench, 1, "S2")
    nc = _nc(bench)
    cap_s = REC_MAX_MS / 1000
    problems = []
    with rec_guard(bench, nc) as rg:
        name = rg.name("X")
        a, b, c = marker("Xa"), marker("Xb"), marker("Xc")
        pm, nm = l12.mark(), nc.dev.mark()
        _rec_start(nc, name)
        t0 = time.monotonic()
        _ack_ok(nc.test_action(_act(a)), "first marker")
        time.sleep(max(0.0, t0 + cap_s * BEFORE_CAP - time.monotonic()))
        _ack_ok(nc.test_action(_act(b)), "second marker")
        time.sleep(max(0.0, t0 + cap_s - 1.5 - time.monotonic()))
        _, ts = _await_line(nc, nm, rf"^\[REC\] capped at {int(cap_s)}s — saved clip '{re.escape(name)}'$",
                            max(1.0, t0 + cap_s + 8 - time.monotonic()))
        _ack_ok(nc.test_action(_act(c)), "third marker")
        got = _wait_all(l12, pm, [a, b, c], 3.0)
        state = _state(nc)
        listed = _ls(nc).get(name)
        evs = nc.rec_download(name)
    took = ts - t0
    if not cap_s - 1.0 <= took <= cap_s + 2.0:
        problems.append(f"the cap line came {took:.1f} s after the take started; the cap is {cap_s:g} s")
    lost = [t for t in (a, b, c) if t.encode() + b"\r" not in got]
    if lost:
        problems.append(f"markers {lost} never reached W1 S2")
    if state != ("idle", 2):
        problems.append(f"after the cap ?REC,INFO shows {state}; expected idle with 2 events")
    cmds = [e.get("cmd") for e in evs]
    if cmds != [f";S2{a}", f";S2{b}"]:
        problems.append(f"the capped clip holds {cmds}; expected the two markers sent before the cap")
    elif listed != (clip_bytes(2), evs[-1]["t"], 2) or not evs[-1]["t"] < REC_MAX_MS:
        problems.append(f"?REC,LS lists the capped clip as {listed}")
    assert not problems, "; ".join(problems)


# ============================================================ clips saved before skipRunning
@test("ncrec.v1_clip_migration", "A clip saved before RcAction grew skipRunning (136-byte records: 16 + 136 n bytes) "
      "loads through the in-memory migration: '[REC] '<name>': migrated <n> events', a ranged download of all n "
      "events that are time-ordered, end at the listed duration and decode (keyframes on slot 1-8, channel 0-31; "
      "actions of known types); a current 140-byte clip loads without it; neither file changes (reads only)",
      needs=["navicore"], links=[])
def v1_clip_migration(bench):
    """loadClip derives the record stride from the file size (navicore_record.h:609-615): a body of n x 136 bytes is a
    clip from before skipRunning was inserted mid-struct (:41-58), read one record at a time through _migrateV1Event
    (:97-112, :626-642), which prints '[REC] '<name>': migrated <n> events from the pre-skipRunning clip format'
    (:640-641); a body of n x 140 is read straight (:618-625). Loading never writes the file (:56-58). The bench NaviCore
    holds such clips (rec_1 to rec_5, long, repeat and test list 16 + 136 n bytes in run 20260929-120840), so the plan's
    host test for the migration (NAVICORE.md §6.1) is a bench read here. The smallest clip of each stride is
    downloaded (hil/navicore.py rec_download: every index, fc = count, the fingerprint steady) and checked against its
    listing (clip_event_problems). Greg's clips are only read: the buffer they load into was empty, and ?REC,CLEAR empties
    it again; rec_guard proves every listing unchanged."""
    nc = _nc(bench)
    problems, notes = [], []
    with rec_guard(bench, nc) as rg:
        by = {}
        for name, (nbytes, dur, n) in rg.before.items():
            stride = clip_stride(nbytes, n)
            if stride:
                by.setdefault(stride, []).append((nbytes, name))
        if EVENT_BYTES_V1 not in by:
            raise Skip("no clip on NaviCore is in the pre-skipRunning 136-byte format")
        for stride in (EVENT_BYTES_V1, EVENT_BYTES):
            if stride not in by:
                notes.append(f"no {stride}-byte clip to compare")
                continue
            name = min(by[stride])[1]
            nbytes, dur, n = rg.before[name]
            m = nc.dev.mark()
            evs = nc.rec_download(name)
            said = [x for x in _rlines(nc.dev.since(m)) if x.startswith(f"[REC] '{name}': migrated ")]
            probs, ev_notes = clip_event_problems(evs, n, dur)
            problems += [f"{name} ({stride}-byte records): {p}" for p in probs]
            notes.append(f"{name}: {stride}-byte records, {n} events; " + "; ".join(ev_notes))
            want = f"[REC] '{name}': migrated {n} events from the pre-skipRunning clip format"
            if stride == EVENT_BYTES_V1 and want not in said:
                problems.append(f"{name}: loading it printed {said or 'no migration line'}; expected '{want}'")
            if stride == EVENT_BYTES and said:
                problems.append(f"{name}: a 140-byte clip printed {said}")
    bench.note("ncrec.v1_clip_migration: " + "; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ (should) findings
@test("ncrec.replay_only_clip_channels", "(should) A replay resets speed and accel and re-sends a target only on the "
      "channels its clip drives: a take of one W1 S2 marker, played after ?MAE,<slot 4>,<ch>,6400 set that remote "
      "channel's pose, must send nothing to it (today: speed 0, accel 0 and target 6400 on W1 S1; RAM only)",
      needs=["navicore", "wcb1"], links=["W1S1", "W1S2"])
def replay_only_clip_channels(bench):
    """NAVICORE.md D-NC64. _buildCurveIndex marks every channel whose last-commanded position NaviCore knows as active
    (cv.active = known, navicore_record.h:371-376), and for every active channel sends speed 0 and accel 0 (:395,
    through recCbResetChan, NaviCore.ino:2194) and the known position again (navicore_record.h:396), whether the clip
    drives it or not; the clip's keyframes only add channels (:380-389). RECORD_REPLAY_DESIGN.md resets 'every
    (slot,ch) the clip drives' (§3, precondition 1; §6). So replaying any clip - here one with no Maestro event at all -
    zeroes the easing of every servo NaviCore has moved since boot, the dome's J2 channel among them on this bench. The
    completion re-applies only knob-managed channels (reapplyMaestroEasing, NaviCore.ino:1147-1170, from :5489); a
    channel with limits set in the Maestro's own settings loses them, the very write reassertMaestroEasing refuses
    (:1198-1210); and a channel a Maestro script moved since (maestroRestartScript leaves the shadow as it was,
    :1085-1089) snaps back to the stale position at full speed. The channel here is a free one on remote slot 4 (device
    4, which nothing hosts) given a pose with ?MAE (maestroSetTarget's shadow, :1047) and invalidated again afterwards
    (a target of 0, :1038-1045). The premise (a normal failure): the replay ran - its marker reached W1 S2."""
    l11, l12 = link(bench, 1, "S1"), link(bench, 1, "S2")
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    (ch,) = _free_channels(cfg, slot, 1)
    hook = _hooks(nc)
    wrote, others = None, []
    with rec_guard(bench, nc) as rg:
        m = marker("O")
        _markers_take(nc, l12, rg.name("O"), [m], ())
        _cli_stop(nc)
        try:
            nc.mae_set(slot, ch, POSE)
            time.sleep(0.5)
            m11, pm, nm = l11.mark(), l12.mark(), nc.dev.mark()
            with nc.debug(DBG_WIRE if hook else 0):
                started = _play(nc)
                _await_line(nc, nm, PLAYBACK_DONE, 4.0)
                time.sleep(0.6)
                lines = _flushed(nc, nm, 0.05)
            ran = m.encode() + b"\r" in l12.received(pm)
            got = [(c, x, v) for _, c, x, v in timed_frames(l11.bursts(m11), dev, {ch})]
            others = sorted({(d, x) for _, d, c, x, v in all_frames(l11.bursts(m11)) if (d, x) != (dev, ch)})
            if hook:
                data, gaps = wire_log(lines, "WCBStream")
                wrote = None if gaps else [(c, x, v) for _, c, x, v in timed_frames([(0, data)], dev, {ch})]
        finally:
            nc.mae_set(slot, ch, 0)
    bench.note(f"ncrec.replay_only_clip_channels: slot {slot} (device {dev}) ch {ch}; the replay's preamble also reached "
               f"(device, channel) {others or 'nothing else'} on W1 S1")
    assert started.startswith("[REC] replaying 1 events over "), f"?REC,PLAY answered {started!r}"
    assert ran, "the replay's marker never reached W1 S2: the replay did not run"
    assert not got, (
        f"(should, D-NC64) a replay of a take with no Maestro event sent {got} to slot {slot} ch {ch}, a channel "
        f"outside the clip whose pose ?MAE had set"
        + (f" (NaviCore's [WIRE] copy: {wrote})" if wrote is not None else "")
        + ": _buildCurveIndex resets speed/accel and re-sends the target of every channel with a known pose "
          "(navicore_record.h:371-376, :395-396), not only the clip's; reset only the channels the clip drives")


@test("ncrec.busy_load_not_missing", "(should) While the recorder is busy, asking for a clip ?REC,LS lists is answered "
      "as busy, not 'not found': ?REC,EDITLOAD,<clip>,0,0,B during a take and during a replay (today '[REC] clip "
      "'<name>' not found' both times, and the config tool's backup waits out 15 s a clip; the clip asked for is only "
      "read, never loaded; RAM-only take)", needs=["navicore", "wcb1"], links=["W1S2"])
def busy_load_not_missing(bench):
    """NAVICORE.md D-NC65. loadClip returns false when the recorder is not idle (navicore_record.h:595), the same false it
    returns for a clip that is not there (:597-601), and every caller reports both as missing: EDITLOAD prints '[REC]
    clip '<name>' not found' (NaviCore.ino:3550-3556), as ?REC,PLAY,<name> does (:3469). The config tool's ranged
    download does not read that line (_clipRangeFeed takes [CLIPDL:*] lines only, config_tool/index.html:6553-6599), so
    a backup started while a clip plays - an idle-animation loop, say - waits out its 15 s budget on every clip but the
    resident one and skips each as 'timed out after 15s waiting for the board' (clipDownloadVerified :6636-6700, the
    message :6691; the backup loop :6808-6817). The (should): the reply says the recorder is busy (a [CLIPDL:ERR] line,
    which the tool's ranged download already turns into an error, :6582, or any line saying busy), or serves the range;
    never 'not found' for a listed clip. The clip asked for is the smallest listed one, anyone's: EDITLOAD only reads,
    and here it is refused anyway. Nothing plays it: ?REC,PLAY,<name> is not sent (a fix that made it play would move
    servos)."""
    l12 = link(bench, 1, "S2")
    nc = _nc(bench)
    replies = {}
    with rec_guard(bench, nc) as rg:
        if not rg.before:
            raise Skip("no saved clip on NaviCore to ask for")
        target = min(rg.before, key=lambda k: (rg.before[k][0], k))
        m0, m1 = marker("Y0"), marker("Y1")
        _markers_take(nc, l12, rg.name("Y"), [m0], ())
        replies["recording"] = _ask(nc, target)
        _ack_ok(nc.test_action(_act(m1)), "second marker")
        time.sleep(1.5)
        _cli_stop(nc)
        nm = nc.dev.mark()
        started = _play(nc)
        replies["replaying"] = _ask(nc, target)
        _await_line(nc, nm, PLAYBACK_DONE, 5.0)
    assert started.startswith("[REC] replaying 2 events over "), f"?REC,PLAY answered {started!r}"
    bad = {k: v for k, v in replies.items() if any(x == f"[REC] clip '{target}' not found" for x in v)}
    unclear = {k: v for k, v in replies.items() if k not in bad and not _busy_or_served(v, target)}
    assert not unclear, f"EDITLOAD of {target} while {', '.join(unclear)} got no clear reply: {unclear}"
    assert not bad, (
        f"(should, D-NC65) ?REC,EDITLOAD,{target},0,0,B while the recorder was {' and '.join(bad)} answered '[REC] clip "
        f"'{target}' not found' for a clip ?REC,LS lists: loadClip returns false when the recorder is busy "
        f"(navicore_record.h:595) and EDITLOAD reports it as missing (NaviCore.ino:3550-3556); say busy "
        f"([CLIPDL:ERR]), which the config tool already handles")


def _ask(nc, name, window=1.5):
    """?REC,EDITLOAD,<name>,0,0,B and its #L12 poke -> the lines of the next `window` s, right-stripped."""
    m = nc.dev.mark()
    nc.dev.send(f"?REC,EDITLOAD,{name},0,0,B")
    nc.dev.send("#L12")
    time.sleep(window)
    return [x for x in _rlines(nc.dev.since(m)) if x.startswith(("[REC]", "[CLIPDL:"))]


def _busy_or_served(lines, name):
    return any("busy" in x.lower() or x.startswith("[CLIPDL:ERR]") for x in lines) or \
        any(x.startswith("[CLIPDL:BEGIN]") and f'"nm":"{name}"' in x for x in lines)


@test("ncrec.editcancel_empties", "(should) The recorder's discard verbs do what they say: ?REC,EDITCANCEL after two "
      "EDITEVs leaves the buffer empty (today 'CANCEL,OK' with both events kept, which ?REC,PLAY would play and "
      "?REC,SAVE save), and ?REC,CLEAR during a take never answers 'cleared' while the take goes on (RAM only)",
      needs=["navicore", "wcb1"], links=["W1S2"])
def editcancel_empties(bench):
    """NAVICORE.md D-NC66. editCancel only drops ST_EDITING to idle (navicore_record.h:950), though its comment says it
    'discards whatever was staged' (:948-949) and stop()'s says abandoning an upload is its job because a partial
    buffer left behind is 'exposed to a following STOP+SAVE' (:316-323): _count keeps the staged events (editAddEvent
    :922-923), so ?REC,INFO shows them, ?REC,PLAY plays them (NaviCore.ino:3468-3474) and ?REC,SAVE saves them
    (saveClip gates on idle only, navicore_record.h:539-540); the config tool sends EDITCANCEL after a failed upload
    (config_tool/index.html:6957, :7183, :7367). ?REC,CLEAR prints '[REC] cleared' whatever clearClip did
    (NaviCore.ino:3605), and clearClip clears only when idle (navicore_record.h:308), so during a take it answers
    'cleared' and the take goes on. The (should): EDITCANCEL empties the buffer;
    CLEAR either empties it or says it did not. The upload's events are W1 S2 markers, never played here; the take is
    RAM only, ended by ?REC,STOP."""
    l12 = link(bench, 1, "S2")
    nc = _nc(bench)
    problems, premise = [], []
    with rec_guard(bench, nc) as rg:
        begin = [x for x in _rlines(nc.cli("?REC,EDITBEGIN", until=r"^\[CLIPUL:BEGIN,")) if x.startswith("[CLIPUL:BEGIN,")]
        for i in (0, 1):
            ev = dict(_act(marker(f"E{i}")), t=100 * i, k=REC_ACTION)
            acked = [x for x in _rlines(nc.cli(f"?REC,EDITEV,{i},{json.dumps(ev, separators=(',', ':'))}",
                                               until=rf"^\[CLIPUL:(ACK,{i}\]|NAK)"))
                     if x.startswith("[CLIPUL:")]
            if acked[:1] != [f"[CLIPUL:ACK,{i}]"]:
                premise.append(f"EDITEV {i} answered {acked}")
        cancel = [x for x in _rlines(nc.cli("?REC,EDITCANCEL", until=r"^\[CLIPUL:CANCEL,")) if x.startswith("[CLIPUL:")]
        after_cancel = _state(nc)
        emptied = _clear(nc)
        _markers_take(nc, l12, rg.name("Z"), [marker("Z")], ())
        clear_reply = [x for x in _rlines(nc.cli("?REC,CLEAR", until=r"^\[REC\] ")) if x.startswith("[REC] ")]
        during = _state(nc)
        _cli_stop(nc)
    if begin != ["[CLIPUL:BEGIN,OK]"]:
        premise.append(f"EDITBEGIN answered {begin}")
    if cancel != ["[CLIPUL:CANCEL,OK]"]:
        premise.append(f"EDITCANCEL answered {cancel}")
    if emptied != ("idle", 0):
        premise.append(f"?REC,CLEAR while idle left {emptied}")
    assert not premise, "; ".join(premise)
    if after_cancel != ("idle", 0):
        problems.append(f"after EDITBEGIN, two EDITEVs and EDITCANCEL ('{cancel[0]}') ?REC,INFO shows {after_cancel}: "
                        f"the staged events stay in the buffer (editCancel only drops the state, "
                        f"navicore_record.h:950), where ?REC,PLAY plays and ?REC,SAVE saves them")
    if "[REC] cleared" in clear_reply and (during[0] != "idle" or during[1]):
        problems.append(f"?REC,CLEAR during a take answered '[REC] cleared' while ?REC,INFO still shows {during} "
                        f"(clearClip clears only when idle, navicore_record.h:308; the CLI prints 'cleared' "
                        f"regardless, NaviCore.ino:3605)")
    assert not problems, "(should, D-NC66) " + "; ".join(problems)
