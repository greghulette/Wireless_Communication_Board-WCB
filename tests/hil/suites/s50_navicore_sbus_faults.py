"""NaviCore's SBUS reader and RC engine under the faults a real receiver produces and the bench controller never sends on
its own: the failsafe and lost-frame flags, a dead link, the 16-channel frame, malformed bursts, a one-frame dip
(docs/hil_plan/NAVICORE.md NC-WP11, ids sbus.*). NaviCore's ten suite numbers s40-s49 are taken; this is the SBUS half
of the engine's faults, after s42.

The inputs are the SBUS controller's RAM-only test verbs (NAVICORE.md INF8, D-NC8; SBUSController's local branch
hil-week, never its main): the flags byte, the stream stopped or let through a set number of frames, SBUS-16 for one
boot, one malformed burst in place of a frame, a raw channel value (hil/sbus.py SbusCtl). Every test asks for them
first ({"t":"flags"} changes nothing, and an image without the verbs never answers it) and skips naming the image when
they are missing, before it writes anything: on such an image the frame-format verb would save. A test state some cut-
off test left changed is put back first (_setup), and every test ends with the controller as it found it (_put_back):
the test state as a reset leaves it, the sticks centred, and every channel NaviCore reads as it was.

How the tests see NaviCore. The failsafe and lost-frame tests borrow a free knob on the rx stick (frames for remote
Maestro slot 4 on W1 S1: device 4 is hosted nowhere, so nothing moves) and a free switch on the ry stick (a marker per
position on W1 S2), inside nc_guard, plus a controller matrix button whose slot has no mapping (rc_trig only), as s42
does. NaviCore prints each dispatch as '{"sys":1,"type":"rc_trig","id":20,"mode":1,"btn":19,"tap":1}' (rcDispatch,
NaviCore.ino:2225-2275), and s42 _taps reads them for one (mode, slot) as [(host time, tap number)]. The glitch tests
stop the stream, send one burst, then let good frames through one at a time: NaviCore's #L09 counts each loop pass that
decoded a frame (one per frame here, since they come one at a time) and #L13 keeps the last frame's bytes, and with the
channels at rest every frame the controller sends is the same, so any other bytes in #L13 are a frame nobody sent.
What each burst should decode to is hil/sbus.py ReaderModel, NaviCore's framing byte for byte. Around a burst that
could decode garbage the engine is made inert first (s41 _engine_inert), and around SBUS-16 NaviCore's bindings on
CH17-24, which then read 992, are unbound (_hi_inert); nc_guard puts both back.

Every test here moves or stops what NaviCore re-emits on SBUS OUT (sbusOutEnabled; the tee copies every byte, a
malformed one included) and the controller's own RC PWM outputs follow CH1-4, so all of them are in hil/servos.py.
"""
import json
import time
from contextlib import contextmanager

from hil.nc_guard import nc_guard
from hil.navicore import NaviCore, SBUS_FULL_FPS
from hil.ncmesh import bridged
from hil.runner import Skip, test
from hil.sbus import (FLAG_FAILSAFE, FLAG_LOST, FRAME_LEN, HEADER, FOOTER, SBUS_CENTER, ReaderModel, SbusCtl, decode,
                      glitch_bursts, reader_decodes)
from suites.common import link, marker, usb_wcb
from suites.s41_navicore_engine import FLAGS_ALL, _count, _engine_inert, _remote_slot, _wait_all
from suites.s42_navicore_sbus_engine import (AXIS_INDEX, CMD_TARGET, Sticks, _frames, _free_channels, _free_knobs, _free_switches,
                                             _knob, _monitor_frames, _out, _press, _samples_ok, _stick, _sw_tiers,
                                             _taps, _unmapped_buttons, _wait_frames, knob_pos)

STICKS = ("rx", "ry", "ly", "lx")      # getcfg names each stick's channel under these keys (SBUSController buildCfgJson)
KNOB_LO, KNOB_HI = 4000, 8000          # the borrowed knob's output range (s42 _out's default)


# ------------------------------------------------------------------ the controller: verbs, setup, put-back
def _verbs(ctl):
    """The controller's INF8 test state (SbusCtl.test_state), or Skip naming its image. Asked before anything else: on
    an image without the verbs "mode" with "save":false would save the frame format."""
    st = ctl.test_state()
    if st is None:
        try:
            ver = ctl.ping_once()
        except AssertionError:
            ver = "an image that answers no ping"
        raise Skip(f"the SBUS controller runs {ver}, which has no INF8 test verbs: flash SBUSController's hil-week "
                   f"image to it, app only (NAVICORE.md INF8)")
    return st


def _dirty(st):
    """True when the test state is not what a reset leaves."""
    return bool(st.get("flags")) or not st.get("stream") or st.get("budget", -1) != -1 or st.get("sbus24") != st.get(
        "saved24")


def _setup(bench):
    """(SbusCtl with the verbs armed, NaviCore, the controller's getcfg, NaviCore's GET_CONFIG), or Skip: on a controller
    without the INF8 verbs, and unless NaviCore reads a full-rate SBUS-24 stream (s21 _sbus_setup's gate, after the
    probe). A test state a cut-off test left changed is put back first, and noted."""
    ctl, nc = SbusCtl(bench.dev("sbus")), NaviCore(bench.dev("navicore"))
    st = _verbs(ctl)
    if _dirty(st):
        bench.note(f"the controller's test state was left changed ({st}); put back: {ctl.clear_faults()}")
        time.sleep(1.0)
    state = nc.sbus_full_rate()
    if state["fps"] < SBUS_FULL_FPS or state["variant"] != "SBUS-24":
        raise Skip(f"NaviCore sees no full-rate SBUS-24 stream (fps {state['fps']}, {state['variant']})")
    return ctl, nc, ctl.cfg(), nc.config()


def _put_back(ctl, nc, cfg, found, problems):
    """The end of every test here. The test state as a reset leaves it (clear_faults), the sticks centred when NaviCore
    reads one off where it was (a frame-format change resets them to 992, SBUSController "mode"), and then every channel
    NaviCore reads as it was at the start (`found`, #L09): one that is not goes into `problems`."""
    try:
        ctl.clear_faults()
    except AssertionError as e:
        problems.append(f"the controller's test state could not be put back: {e}")
    time.sleep(0.5)
    now = nc.sbus_dump()["channels"]
    sticks = [cfg[a] for a in STICKS if isinstance(cfg.get(a), int) and 1 <= cfg[a] <= min(len(now), len(found))]
    if any(now[c - 1] != found[c - 1] for c in sticks):
        ctl.axes(0, 0, 0, 0)
        time.sleep(0.5)
        now = nc.sbus_dump()["channels"]
    moved = [f"CH{i + 1} {found[i]}->{now[i]}" for i in range(min(len(found), len(now))) if now[i] != found[i]]
    if len(now) != len(found):
        moved.append(f"{len(now)} channels, not {len(found)}")
    if moved:
        problems.append(f"the controller was not left as found: {', '.join(moved)}")


def _frames_count(nc):
    """NaviCore's count of decoded SBUS frames (#L09 frames=)."""
    return int(nc.sbus_dump()["frames"] or 0)


def _quiet(ctl, nc):
    """Stop the stream and wait for NaviCore's frame counter to hold still -> the count. AssertionError when it goes on
    rising: the stream did not stop, and nothing counted after it would mean anything."""
    ctl.stream(False)
    time.sleep(0.15)
    a = _frames_count(nc)
    time.sleep(0.25)
    b = _frames_count(nc)
    if a != b:
        raise AssertionError(f"with the stream off NaviCore's frame counter still rose ({a} -> {b})")
    return b


# ------------------------------------------------------------------ NaviCore's side: guards and telemetry
@contextmanager
def _inert(bench):
    """nc_guard with NaviCore's SBUS side unable to act (s41 _engine_inert: every band 0/0, no mode function, every knob
    off), for a burst that could make it decode a frame of garbage. A switch tier needs switchSettleMs of one position,
    and the next frame NaviCore decodes is a real one, which cancels the candidate (processSwitches, NaviCore.ino:2453).
    -> the guard."""
    with nc_guard(bench) as g:
        _engine_inert(g.nc, g.before)
        time.sleep(1.0)
        yield g


@contextmanager
def _hi_inert(bench, ncfg):
    """For a test that switches the controller to SBUS-16: NaviCore then reads CH17-24 as 992 (sbus_reader.h decodeFrame
    :283), so a switch bound there fires the tier of its middle band and a knob drives its outputs to mid-range (on this
    bench switch SJ's text tiers, and the dome's RS and J2 knobs). Inside nc_guard every switch and every knob on CH17-24
    is unbound (channel 0, the rest of each object as it was); with none there, no guard and no write. -> (the guard or
    None, the labels unbound)."""
    sw = sorted(lab for lab, s in (ncfg.get("switches") or {}).items() if 17 <= (s.get("channel") or 0) <= 24)
    kn = sorted(lab for lab, k in (ncfg.get("knobs") or {}).items() if 17 <= (k.get("channel") or 0) <= 24)
    if not sw and not kn:
        yield None, []
        return
    with nc_guard(bench) as g:
        change = {}
        if sw:
            change["switches"] = {lab: dict(g.before["switches"][lab], channel=0) for lab in sw}
        if kn:
            change["knobs"] = {lab: dict(g.before["knobs"][lab], channel=0) for lab in kn}
        g.nc.set_config(change)
        time.sleep(1.0)
        yield g, sw + kn


def _sys_lines(lines):
    """The '{"sys":1,...}' JSON lines among `lines`, parsed; one that does not parse is left out."""
    out = []
    for x in lines:
        if x.startswith('{"sys":1'):
            try:
                out.append(json.loads(x))
            except ValueError:
                pass
    return out


def _relayed(w1, kind, enough, timeout):
    """NaviCore's `kind` telemetry lines as W1 relays them -> [dict]. A bridged PING opens W1's 20 s relay window and, as
    inbound JSON, subscribes W1 to rc_ch for 15 s (rc_telemetry.h _hasWcbSubscriber :1014-1017); rc_hb goes out every
    2 s regardless (:1667-1691). Both are best-effort broadcasts, so a missing line proves nothing: the callers note it.
    Returns once `enough(lines)` is true or `timeout` has passed."""
    m = w1.dev.mark()
    bridged(w1, {"type": "PING"}, r'"type":"PONG"', timeout=3.0)
    deadline = time.monotonic() + timeout
    while True:
        got = [o for o in _sys_lines(w1.dev.since(m)) if o.get("type") == kind]
        if enough(got) or time.monotonic() >= deadline:
            return got
        time.sleep(0.2)


def _io_rig(g, rx, ry, slot):
    """Inside nc_guard `g`: a free knob on the rx stick's channel passing through to channel x of remote Maestro slot
    `slot` (its frames reach W1 S1; the device is hosted nowhere, so nothing moves), and a free switch on the ry stick's
    channel firing one W1 S2 marker per position -> (x, {position: marker}). The ry stick rests in the switch's middle
    band, which the save seeds without firing. Skip without a free knob and a free switch."""
    knobs, switches = _free_knobs(g.before), _free_switches(g.before)
    if not knobs or not switches:
        raise Skip("no free knob and free switch to borrow")
    (x,) = _free_channels(g.before, slot, 1)
    tags = {p: marker(f"Q{p}") for p in range(3)}
    g.nc.set_config({"knobs": {knobs[0]: _knob(rx, outputs=[_out(x, KNOB_LO, KNOB_HI, target=slot)])},
                     "switches": {switches[0][1]: {"channel": ry, "positions": 3, **_sw_tiers(tags)}}})
    time.sleep(1.0)
    return x, tags


def _knob_frame(v, x):
    return [(CMD_TARGET, x, knob_pos(v, KNOB_LO, KNOB_HI))]


def _high_band(cfg, axis):
    """A value the controller's `axis` stick reaches that a 3-position switch reads as its high position (over 1401,
    NaviCore readSwitchPos), or Skip when the stick's calibrated range (getcfg aMin/aMax) stops short of it."""
    i = AXIS_INDEX[axis]
    top = max(cfg["aMin"][i], cfg["aMax"][i])
    for v in (1500, 1450, 1420):
        if v <= top:
            return v
    raise Skip(f"the {axis} stick tops out at {top}, short of a switch's high band (over 1401)")


def _switch_pos(v, positions):
    """NaviCore's readSwitchPos (NaviCore.ino:566-571): a 2-position switch is high above 900; a 3-position one has its
    band edges at 582 and 1401."""
    if positions == 2:
        return 2 if v > 900 else 0
    return 0 if v < 582 else (2 if v > 1401 else 1)


# ------------------------------------------------------------------ the glitch bracket
def _truncations(frame):
    """Every cut 1..len-1 of `frame`, sorted by what NaviCore's framing (ReaderModel) makes of the cut and one good frame
    after it -> (phantom: it decodes a frame nobody sent; clean: it decodes nothing from the cut and ends idle; later: it
    swallows the good frame too, then recovers within six more, every decode real)."""
    phantom, clean, later = [], [], []
    for n in range(1, len(frame)):
        dec1, m1 = reader_decodes(frame, [frame[:n]], 1)
        if any(d != frame for d in dec1):
            phantom.append(n)
            continue
        dec6, _ = reader_decodes(frame, [frame[:n]], 6)
        if all(d == frame for d in dec6):
            (clean if m1.idle else later).append(n)
    return phantom, clean, later


def _glitch_case(ctl, nc, frame, kind, n, steps):
    """One malformed burst with the stream stopped, then `steps` good frames let through one at a time -> {'steps':
    [(frames NaviCore counted, #L13 after)] for the burst alone and then each frame, 'model': what ReaderModel expects
    it to count at each, 'hex': a garbage burst's bytes}. NaviCore's #L09 counts a loop pass whose read() decoded any
    frame, once (processSbus, NaviCore.ino:2757-2764), so the model counts a burst's decodes as one: a double burst drained
    in one pass counts 1 (run 20260929-201950), though a pass that splits it would count 2. The stream is back on when
    it returns."""
    f = _quiet(ctl, nc)
    st = ctl.glitch(kind, n)
    time.sleep(0.12 + (n / 1000 if kind == "gap" else 0))
    got = []
    for k in range(steps + 1):
        if k:
            ctl.stream(True, frames=1)
            time.sleep(0.12)
        f2 = _frames_count(nc)
        got.append((f2 - f, nc.sbus_raw()))
        f = f2
    ctl.stream(True)
    m = ReaderModel(24 if len(frame) == FRAME_LEN[24] else 16)

    def counted(burst):
        before = len(m.decoded)
        m.burst(burst).silence()
        return int(len(m.decoded) > before)
    model = [sum(counted(b) for b in glitch_bursts(kind, n, frame, st.get("hex")))]
    model += [counted(frame) for _ in range(steps)]
    return {"steps": got, "model": model, "hex": st.get("hex")}


def _describe(raw, frame):
    """A frame NaviCore decoded, against the frame the controller sends, for a message: its bytes, its flags, and the
    channels it holds that the controller never sent."""
    if len(raw) not in FRAME_LEN.values() or raw[0] != HEADER or raw[-1] != FOOTER:
        return f"{raw.hex().upper()} ({len(raw)} bytes)"
    d, want = decode(raw), decode(frame)
    off = [f"CH{i + 1} {want['channels'][i]}->{v}" for i, v in enumerate(d["channels"][:want["n"]])
           if v != want["channels"][i]]
    return (f"{raw.hex().upper()}: flags {d['flags']:#04x}{' (failsafe)' if d['failsafe'] else ''}, {len(off)} of "
            f"{d['n']} channels off what the controller sends ({', '.join(off[:4])}{', ...' if len(off) > 4 else ''})")


# ============================================================ the verbs themselves
@test("sbus.test_verbs", "The SBUS controller's INF8 test verbs act on the wire as NaviCore reads it: the flags byte "
      "shows as lost/failsafe, the stream stops and starts with no reset, a budget of 5 lets exactly 5 frames through, a "
      "raw value holds until the stick writes its channel again, a dip is one frame at 992; bad arguments are refused "
      "and send nothing (the rx stick's channel, bound to nothing)", needs=["sbus", "navicore"], links=[])
def test_verbs(bench):
    """INF8's acceptance on the bench, the first test to run after the controller is flashed with the hil-week image
    (SBUSController.ino processCommandJson "flags", "stream", "ch", "glitch"; sendGlitch; loop()). NaviCore's #L09 gives
    the flags of the last frame it decoded and counts every frame (dumpSbusState, NaviCore.ino:2897-2915), and #L13
    keeps the last one's bytes, so a stopped stream is a counter that holds still and a budget that many more frames.
    The frame-format verb is sbus.sbus16_autodetect's, where NaviCore's bindings on CH17-24 are unbound first. The
    failsafe flag freezes NaviCore's dispatch while it is on, and nothing moves meanwhile anyway."""
    ctl, nc, cfg, ncfg = _setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    found = nc.sbus_dump()["channels"]
    st0 = dict(ctl.verbs)
    problems, notes = [], []
    try:
        prod = (st0.get("flags"), st0.get("stream"), st0.get("budget"), st0.get("sbus24"), st0.get("saved24"))
        if prod != (0, True, -1, True, True) or cfg.get("sbus24") is not True:
            problems.append(f"the probe reads {st0} and getcfg sbus24 {cfg.get('sbus24')}: not a controller streaming "
                            f"SBUS-24 with flags 0 as saved")
        for v, want in ((FLAG_LOST, ("YES", "no")), (FLAG_FAILSAFE, ("no", "YES")),
                        (FLAG_LOST | FLAG_FAILSAFE, ("YES", "YES")), (0, ("no", "no"))):
            ctl.flags(v)
            time.sleep(0.3)
            d = nc.sbus_dump()
            if (d["lost"], d["failsafe"]) != want:
                problems.append(f"flags {v:#04x}: NaviCore reads lost={d['lost']} failsafe={d['failsafe']}, expected "
                                f"lost={want[0]} failsafe={want[1]}")
        f0 = _quiet(ctl, nc)
        a = nc.sbus_dump()
        time.sleep(0.6)
        b = nc.sbus_dump()
        if int(b["frames"] or 0) != f0 or b["age"] <= a["age"]:
            problems.append(f"with the stream off NaviCore's counter went {f0} -> {b['frames']} and ageMs "
                            f"{a['age']} -> {b['age']}")
        ctl.stream(True, frames=5)
        time.sleep(0.3)
        f1 = _frames_count(nc)
        st = ctl.test_state()
        if f1 - f0 != 5:
            problems.append(f"a budget of 5 frames: NaviCore decoded {f1 - f0}")
        if st.get("stream") or st.get("budget") != 0:
            problems.append(f"after its budget the stream did not stop: {st}")
        # Bad arguments, with the stream off so anything a refused verb sent would still show as a frame.
        refused = ({"t": "flags", "v": 256}, {"t": "flags", "v": -1}, {"t": "stream", "on": True, "frames": 0},
                   {"t": "stream", "on": True, "frames": 1001}, {"t": "ch", "c": 0, "v": 5}, {"t": "ch", "c": 25, "v": 5},
                   {"t": "ch", "c": ch, "v": 2048}, {"t": "glitch", "kind": "truncate", "n": 36},
                   {"t": "glitch", "kind": "garbage", "n": 65}, {"t": "glitch", "kind": "double", "n": 1},
                   {"t": "glitch", "kind": "gap", "n": 51}, {"t": "glitch", "kind": "dip", "n": 25},
                   {"t": "glitch", "kind": "zap", "n": 3})
        for obj in refused:
            r = ctl.test_verb(obj, check=False)
            if r.get("ok"):
                problems.append(f"{json.dumps(obj, separators=(',', ':'))} was accepted")
        time.sleep(0.2)
        st = ctl.test_state()
        if _frames_count(nc) != f1 or (st.get("flags"), st.get("stream"), st.get("budget")) != (0, False, 0):
            problems.append(f"a refused verb changed something: {_frames_count(nc) - f1} frame(s), state {st}")
        # A raw value, and the dip: one frame with the channel at 992, alone with the stream off.
        ctl.channel(ch, 700)
        ctl.glitch("dip", ch)
        time.sleep(0.15)
        f2 = _frames_count(nc)
        dip = decode(nc.sbus_raw())
        if f2 - f1 != 1 or dip["channels"][ch - 1] != SBUS_CENTER:
            problems.append(f"the dip: {f2 - f1} frame(s), CH{ch} = {dip['channels'][ch - 1]} in the last one "
                            f"(expected one frame at {SBUS_CENTER})")
        ctl.stream(True)
        d = nc.sbus_full_rate()
        if d["channels"][ch - 1] != 700:
            problems.append(f"after the dip CH{ch} reads {d['channels'][ch - 1]}, not the raw 700")
        ctl.axes(0, 0, 0, 0)
        time.sleep(0.4)
        back = nc.sbus_dump()["channels"][ch - 1]
        if back != found[ch - 1]:
            problems.append(f"the stick did not take CH{ch} back from the raw value: {back}, not {found[ch - 1]}")
        notes.append(f"probe {st0}; the dip frame read CH{ch} = {dip['channels'][ch - 1]}")
    finally:
        _put_back(ctl, nc, cfg, found, problems)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.route_isolates", "The SBUS controller's route verb (output B, S4, for a Kyber): routed to the Kyber, a raw "
      "value never reaches NaviCore, which keeps a full-rate stream at rest with no flag; a stopped stream and the "
      "failsafe flag there leave NaviCore's untouched; routed to both, and back to NaviCore, it reads the value (the rx "
      "stick's channel, bound to nothing)", needs=["sbus", "navicore"], links=[])
def route_isolates(bench):
    """The second SBUS output (SBUSController branch kyber-sbus, "route"): the controls go to output A (S5, NaviCore),
    output B (S4, a Kyber) or both, and an output not routed gets no input - A carries the rest frame the controller
    took at boot, so NaviCore stays linked and nothing it reads moves. Only output A is seen here, through NaviCore's
    #L09: output B is checked by the Kyber tests once it is wired. The stream and flags verbs act on the routed output
    only, so with the controls on the Kyber they must not reach NaviCore. Every reply names the route; clear_faults
    puts it back to NaviCore, and _put_back runs it."""
    ctl, nc, cfg, ncfg = _setup(bench)
    if not ctl.has_route():
        raise Skip("the SBUS controller has no second SBUS output: flash SBUSController's kyber-sbus image, app only")
    ch = _stick(nc, cfg, ncfg, "rx")
    found = nc.sbus_dump()["channels"]
    rest = found[ch - 1]
    v = 400 if abs(rest - 400) > 300 else 1600
    problems = []
    try:
        if ctl.test_state().get("route") != "navicore":
            problems.append(f"the controller did not start routed to NaviCore: {ctl.verbs}")
        ctl.route("kyber")
        ctl.channel(ch, v)
        ctl.flags(FLAG_FAILSAFE)
        ctl.stream(False)
        time.sleep(0.5)
        d = nc.sbus_full_rate()
        if d["channels"][ch - 1] != rest:
            problems.append(f"routed to the Kyber, NaviCore read CH{ch} {d['channels'][ch - 1]}, not its rest {rest}")
        if d["fps"] < SBUS_FULL_FPS or (d["lost"], d["failsafe"]) != ("no", "no"):
            problems.append(f"routed to the Kyber with the stream off and failsafe on there, NaviCore's own stream read "
                            f"fps {d['fps']} lost={d['lost']} failsafe={d['failsafe']}")
        ctl.flags(0)
        ctl.stream(True)
        for to in ("both", "navicore"):
            st = ctl.route(to)
            if st.get("route") != to:
                problems.append(f"route {to}: the reply says {st.get('route')}")
            time.sleep(0.4)
            got = nc.sbus_dump()["channels"][ch - 1]
            if got != v:
                problems.append(f"routed to {to}, NaviCore read CH{ch} {got}, not the raw {v}")
        if ctl.test_verb({"t": "route", "to": "elsewhere"}, check=False).get("ok"):
            problems.append('{"t":"route","to":"elsewhere"} was accepted')
    finally:
        _put_back(ctl, nc, cfg, found, problems)
    assert not problems, "; ".join(problems)


# ============================================================ the flags byte
@test("sbus.failsafe_flag_freeze", "With the failsafe flag (0x08) on every frame NaviCore freezes: a knob sends no frame, "
      "a switch fires no tier, a matrix press dispatches nothing, #L09 reads failsafe=YES at full rate and rc_hb "
      "sbusFail; values parked during it never act once it clears, a button held across it needs a release first, "
      "then the knob, the switch and the matrix all act again (remote slot 4 and W1 S2 markers: nothing moves)",
      needs=["sbus", "navicore", "wcb1"])
def failsafe_flag_freeze(bench):
    """The failsafe gate (processSbus, NaviCore.ino:2782-2796): a frame with the failsafe bit updates the telemetry
    (sbusValues, the flags, fps) and then returns before the mode switch, the matrix, the switches and the knobs
    (:2798-2879), resetting the matrix debounce so that a button held across the dropout needs a confirmed neutral
    first, and abandoning a hold. So values the frames carry during it - a receiver's failsafe positions - move nothing
    then, and nothing afterwards when they are back where they were before it: switchPrevPos and lastKnobRaw never saw
    them (:2453, :2650). rc_hb carries sbusFail (rc_telemetry.h:1667-1691), read on W1 (best effort: noted when none
    comes). A borrowed knob on the rx stick and switch on the ry stick, inside nc_guard (_io_rig)."""
    l11, l12 = link(bench, 1, "S1"), link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _setup(bench)
    rx, ry = _stick(nc, cfg, ncfg, "rx"), _stick(nc, cfg, ncfg, "ry")
    high = _high_band(cfg, "ry")
    slot, dev = _remote_slot(ncfg)
    mode = nc.mode()
    (ib, _, bslot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    sticks, w1 = Sticks(ctl, cfg), usb_wcb(bench)
    found = nc.sbus_dump()["channels"]
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        x, tags = _io_rig(g, rx, ry, slot)
        tap_s = g.before.get("tapWindowMs", 500) / 1000
        settle_s = g.before.get("switchSettleMs", 80) / 1000
        try:
            # The rig works with flags 0.
            m11, pm = l11.mark(), l12.mark()
            sticks.set("rx", 1500)
            got = _wait_frames(l11, m11, dev, {x}, 1)
            if got != _knob_frame(1500, x):
                problems.append(f"before the failsafe the knob sent {got}, expected {_knob_frame(1500, x)}")
            sticks.set("ry", high)
            if _count(_wait_all(l12, pm, [tags[2]], 2.0), tags[2]) != 1:
                problems.append("before the failsafe the switch did not fire position 2 once")
            sticks.set("ry", SBUS_CENTER)
            _wait_all(l12, pm, [tags[1]], 2.0)
            time.sleep(tap_s + 0.3)
            # Failsafe: nothing the frames carry acts.
            ctl.flags(FLAG_FAILSAFE)
            time.sleep(0.3)
            d = nc.sbus_full_rate()
            if (d["failsafe"], d["lost"]) != ("YES", "no") or d["fps"] < SBUS_FULL_FPS:
                problems.append(f"under the failsafe flag #L09 reads failsafe={d['failsafe']} lost={d['lost']} "
                                f"fps={d['fps']} (expected YES, no, full rate: the frames still decode)")
            m11, pm, nm = l11.mark(), l12.mark(), nc.dev.mark()
            sticks.set("rx", 600)
            sticks.set("ry", high)
            _press(ctl, ib, 0.12)
            time.sleep(max(tap_s, settle_s) + 1.0)
            fr, marks, trigs = _frames(l11, m11, dev, {x}), l12.received(pm), _taps(nc, nm, mode, bslot)
            if fr:
                problems.append(f"under failsafe the knob sent {fr}")
            if any(_count(marks, tags[p]) for p in range(3)):
                problems.append(f"under failsafe the switch fired: {marks!r}")
            if trigs:
                problems.append(f"under failsafe a matrix press dispatched: {[t for _, t in trigs]}")
            hb = _relayed(w1, "rc_hb", lambda xs: bool(xs), 5.0)
            if not hb:
                notes.append("no rc_hb relayed in 5 s under failsafe (best effort)")
            elif any(o.get("sbusFail") is not True for o in hb):
                problems.append(f"rc_hb under failsafe: {[o.get('sbusFail') for o in hb]} (expected sbusFail true)")
            # Park the values back where they were, hold the button, clear the flag: nothing acts.
            sticks.set("rx", 1500)
            sticks.set("ry", SBUS_CENTER)
            ctl.button(ib, True)
            time.sleep(0.3)
            m11, pm, nm = l11.mark(), l12.mark(), nc.dev.mark()
            ctl.flags(0)
            time.sleep(1.0)
            ctl.button(ib, False)
            time.sleep(tap_s + 1.0)
            fr, marks, trigs = _frames(l11, m11, dev, {x}), l12.received(pm), _taps(nc, nm, mode, bslot)
            if fr:
                problems.append(f"once the failsafe cleared, the knob - back where it was - sent {fr}")
            if any(_count(marks, tags[p]) for p in range(3)):
                problems.append(f"once the failsafe cleared, the switch - back where it was - fired: {marks!r}")
            if trigs:
                problems.append(f"a button held across the failsafe dispatched when it cleared (a confirmed neutral "
                                f"must come first): {[t for _, t in trigs]}")
            if nc.sbus_dump()["failsafe"] != "no":
                problems.append("#L09 still reads failsafe after the flag cleared")
            # Everything acts again.
            m11, pm, nm = l11.mark(), l12.mark(), nc.dev.mark()
            sticks.set("rx", 1200)
            got = _wait_frames(l11, m11, dev, {x}, 1)
            if got != _knob_frame(1200, x):
                problems.append(f"after the failsafe the knob sent {got}, expected {_knob_frame(1200, x)}")
            sticks.set("ry", high)
            if _count(_wait_all(l12, pm, [tags[2]], 2.0), tags[2]) != 1:
                problems.append("after the failsafe the switch did not fire position 2 once")
            _press(ctl, ib, 0.12)
            time.sleep(tap_s + 1.0)
            taps = [t for _, t in _taps(nc, nm, mode, bslot)]
            if taps != [1]:
                problems.append(f"after the failsafe a matrix press gave taps {taps} on slot {bslot} in mode {mode}, "
                                f"expected [1]")
        finally:
            ctl.clear_faults()
            ctl.button(ib, False)
            sticks.center()
            time.sleep(tap_s + 0.5)
    _put_back(ctl, nc, cfg, found, problems)
    if notes:
        bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.lost_frame_flag_no_gate", "With the lost-frame flag (0x04) on every frame NaviCore still acts: a knob sends "
      "its frame, a switch fires its tier, a matrix press dispatches; #L09 reads lost=YES failsafe=no at full rate, the "
      "monitor shows lost:true, rc_hb sbusLost (remote slot 4 and W1 S2 markers: nothing moves)",
      needs=["sbus", "navicore", "wcb1"])
def lost_frame_flag_no_gate(bench):
    """processSbus gates on failsafe only: 'lostFrame is a single-frame transient and gating on it would make control
    feel laggy' (NaviCore.ino:2780-2782). The flag still reaches #L09, the monitor's sbus.lost (and its ok, which reads
    false while the flag is on: sendPWMUpdate :3138; noted) and rc_hb's sbusLost (rc_telemetry.h:1682-1688, noted when
    none is relayed). The same borrowed knob, switch and unmapped matrix button as sbus.failsafe_flag_freeze."""
    l11, l12 = link(bench, 1, "S1"), link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _setup(bench)
    rx, ry = _stick(nc, cfg, ncfg, "rx"), _stick(nc, cfg, ncfg, "ry")
    high = _high_band(cfg, "ry")
    slot, dev = _remote_slot(ncfg)
    mode = nc.mode()
    (ib, _, bslot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    sticks, w1 = Sticks(ctl, cfg), usb_wcb(bench)
    found = nc.sbus_dump()["channels"]
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        x, tags = _io_rig(g, rx, ry, slot)
        tap_s = g.before.get("tapWindowMs", 500) / 1000
        try:
            ctl.flags(FLAG_LOST)
            time.sleep(0.3)
            d = nc.sbus_full_rate()
            if (d["lost"], d["failsafe"]) != ("YES", "no") or d["fps"] < SBUS_FULL_FPS:
                problems.append(f"under the lost-frame flag #L09 reads lost={d['lost']} failsafe={d['failsafe']} "
                                f"fps={d['fps']} (expected YES, no, full rate)")
            m11, pm, nm = l11.mark(), l12.mark(), nc.dev.mark()
            sticks.set("rx", 1500)
            got = _wait_frames(l11, m11, dev, {x}, 1)
            if got != _knob_frame(1500, x):
                problems.append(f"with lost frames flagged the knob sent {got}, expected {_knob_frame(1500, x)}")
            sticks.set("ry", high)
            if _count(_wait_all(l12, pm, [tags[2]], 2.0), tags[2]) != 1:
                problems.append("with lost frames flagged the switch did not fire position 2 once")
            _press(ctl, ib, 0.12)
            time.sleep(tap_s + 1.0)
            taps = [t for _, t in _taps(nc, nm, mode, bslot)]
            if taps != [1]:
                problems.append(f"with lost frames flagged a matrix press gave taps {taps} on slot {bslot} in mode "
                                f"{mode}, expected [1]")
            mon = nc.monitor(1.0)
            flags = {(f["sbus"].get("lost"), f["sbus"].get("failsafe")) for f in mon}
            if not mon:
                notes.append("no monitor frame in 1 s")
            elif flags != {(True, False)}:
                problems.append(f"the monitor's (lost, failsafe) under the flag: {sorted(flags)}")
            notes.append(f"the monitor's ok under lost frames: {sorted({f['sbus'].get('ok') for f in mon})}")
            hb = _relayed(w1, "rc_hb", lambda xs: bool(xs), 5.0)
            if not hb:
                notes.append("no rc_hb relayed in 5 s (best effort)")
            elif any(o.get("sbusLost") is not True or o.get("sbusFail") is not False for o in hb):
                problems.append(f"rc_hb (sbusLost, sbusFail): {[(o.get('sbusLost'), o.get('sbusFail')) for o in hb]}")
            ctl.flags(0)
            time.sleep(0.3)
            if nc.sbus_dump()["lost"] != "no":
                problems.append("#L09 still reads lost after the flag cleared")
        finally:
            ctl.clear_faults()
            ctl.button(ib, False)
            sticks.center()
            time.sleep(tap_s + 0.5)
    _put_back(ctl, nc, cfg, found, problems)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.failsafe_deferred_tap", "(should) Failsafe cancels a matrix tap still waiting to fire: a tap released just "
      "before the failsafe flag comes on, and a press held into it, fire nothing - during the failsafe or once it "
      "clears (unmapped slot: rc_trig only)", needs=["sbus", "navicore"], links=[])
def failsafe_deferred_tap(bench):
    """NAVICORE.md D-NC21. The failsafe gate in processSbus (NaviCore.ino:2782-2796) resets the matrix debounce and
    abandons a hold, but nothing clears the deferred tap (tapState.deferredPending), and checkDeferredTap runs from
    loop() whatever the frames say (:2404-2426, called at :5500). So a tap released just before the failsafe fires
    tapWindowMs after its release, failsafe on; and a press held into the failsafe fires sooner, still held: clearing
    holdActive (:2790) takes away the one thing that parked it (:2412), and it fires tapWindowMs after the press. The
    (should): failsafe cancels any pending tap or hold, and the press must be made again. Each case first checks that
    NaviCore read the flag, and that the flag was on before the tap was due (else it is noted, not judged). Case A
    raises the flag 150 ms after the release: sent at once, it lands in the frame that carries the release, so the gate
    sees the press still held and case A repeats case B (run 20260929-201950: tap 1 456 ms after the release, which is
    tapWindowMs after the press)."""
    ctl, nc, cfg, ncfg = _setup(bench)
    found = nc.sbus_dump()["channels"]
    mode = nc.mode()
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    tap_s = ncfg.get("tapWindowMs", 500) / 1000
    problems, bad, notes = [], [], []
    try:
        # A: a tap released - its neutral frames reach NaviCore first - then the failsafe inside its window.
        nm = nc.dev.mark()
        _, rel = _press(ctl, i, 0.1)
        time.sleep(0.15)
        ctl.flags(FLAG_FAILSAFE)
        on_at = time.monotonic()
        time.sleep(0.2)
        seen = nc.sbus_dump()["failsafe"]
        time.sleep(max(0.0, rel + tap_s + 0.8 - time.monotonic()))
        ctl.flags(0)
        off_at = time.monotonic()
        time.sleep(tap_s + 0.8)
        trigs = _taps(nc, nm, mode, slot)
        early = [t for t in trigs if t[0] < on_at]
        if seen != "YES":
            problems.append(f"case A: NaviCore did not read the failsafe flag (#L09 failsafe={seen})")
        elif early or (on_at - rel) > tap_s - 0.15:
            notes.append(f"case A inconclusive: the failsafe came on {(on_at - rel) * 1000:.0f} ms after the release")
        elif trigs:
            bad.append(f"a tap released {(on_at - rel) * 1000:.0f} ms before the failsafe came on fired tap "
                       f"{trigs[0][1]} {(trigs[0][0] - rel) * 1000:.0f} ms after its release, "
                       f"{'during the failsafe' if trigs[0][0] < off_at else 'after it cleared'}")
        time.sleep(0.5)
        # B: a press held into the failsafe.
        nm = nc.dev.mark()
        ctl.button(i, True)
        press = time.monotonic()
        time.sleep(0.15)
        ctl.flags(FLAG_FAILSAFE)
        time.sleep(0.2)
        seen = nc.sbus_dump()["failsafe"]
        time.sleep(max(0.0, press + tap_s + 0.5 - time.monotonic()))
        ctl.button(i, False)
        released = time.monotonic()
        time.sleep(0.3)
        ctl.flags(0)
        time.sleep(tap_s + 0.8)
        trigs = _taps(nc, nm, mode, slot)
        if seen != "YES":
            problems.append(f"case B: NaviCore did not read the failsafe flag (#L09 failsafe={seen})")
        elif trigs:
            bad.append(f"a press held into the failsafe fired tap {trigs[0][1]} {(trigs[0][0] - press) * 1000:.0f} ms "
                       f"after the press, {'still held' if trigs[0][0] < released else 'after its release'}")
    finally:
        ctl.button(i, False)
        _put_back(ctl, nc, cfg, found, problems)
    if notes:
        bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)
    assert not bad, "(should, D-NC21) " + "; ".join(bad)


# ============================================================ a dead link
@test("sbus.frame_stop_held_press", "(should) A matrix press in flight when the frames stop is forgotten: pressed and "
      "held, the stream stopped, released during the outage, the stream back - NaviCore fires nothing for it "
      "(unmapped slot: rc_trig only)", needs=["sbus", "navicore"], links=[])
def frame_stop_held_press(bench):
    """NAVICORE.md D-NC21. NaviCore has no frame timeout: with no frame nothing reaches processSbus (NaviCore.ino:2757-
    2759), so a press held when the frames stop stays parked (checkDeferredTap :2412) through any outage, and the first
    neutral frames after it are its release (:2825-2837): rcMatrixRelease re-times the tap from then (:2379-2402), and it
    fires tapWindowMs after the link came back - for a button let go seconds before. The outage is the controller's
    "stream" verb: frames stop with no reset, and #L09's frame counter holds still with ageMs rising. The (should): frame
    loss cancels any pending tap or hold, as failsafe should."""
    ctl, nc, cfg, ncfg = _setup(bench)
    found = nc.sbus_dump()["channels"]
    mode = nc.mode()
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    tap_s = ncfg.get("tapWindowMs", 500) / 1000
    problems, bad = [], []
    try:
        nm = nc.dev.mark()
        ctl.button(i, True)
        time.sleep(0.2)
        f0 = _quiet(ctl, nc)
        a = nc.sbus_dump()
        ctl.button(i, False)          # let go while no frame carries it
        let_go = time.monotonic()
        time.sleep(1.2)
        b = nc.sbus_dump()
        during = _taps(nc, nm, mode, slot)
        ctl.stream(True)
        back = time.monotonic()
        time.sleep(tap_s + 1.2)
        after = [t for t in _taps(nc, nm, mode, slot) if t[0] >= back]
        c = nc.sbus_full_rate()
        if int(b["frames"] or 0) != f0 or b["age"] <= a["age"] or b["age"] < 1000:
            problems.append(f"the outage did not show: frames {f0} -> {b['frames']}, ageMs {a['age']} -> {b['age']}")
        if during:
            problems.append(f"rc_trig during the outage: {[t for _, t in during]}")
        if c["fps"] < SBUS_FULL_FPS:
            problems.append(f"the stream did not come back to full rate (fps {c['fps']})")
        if after:
            bad.append(f"the press made before the frames stopped fired tap {after[0][1]} "
                       f"{(after[0][0] - back) * 1000:.0f} ms after they came back, for a button let go "
                       f"{back - let_go:.1f} s before")
    finally:
        ctl.button(i, False)
        _put_back(ctl, nc, cfg, found, problems)
    assert not problems, "; ".join(problems)
    assert not bad, "(should, D-NC21) " + "; ".join(bad)


# ============================================================ SBUS-16
@test("sbus.sbus16_autodetect", "NaviCore follows the controller to SBUS-16 (25-byte frames) at full rate: #L09 SBUS-16 "
      "with CH1-16 as the controller sends them, #L13 25 bytes (flags at byte 23, footer at 24), the monitor's chCount "
      "16 and frameLen 25, CH17-24 read as 992 (rc_ch); then back to SBUS-24 with CH17-24 as they were. The "
      "controller's saved mode never changes", needs=["sbus", "navicore", "wcb1"], links=[])
def sbus16_autodetect(bench):
    """sbus_reader.h: the reader tells the variants apart by length alone, and a 25-byte frame at a silence locks
    SBUS-16 after three in a row (tryParseAndReset :232-265); decodeFrame sets CH17-24 to 992 then (:283). The
    controller's "mode" with "save":false switches the frame format for this boot only and resets its sticks and
    buttons to 992 as a saved switch does (SBUSController.ino processCommandJson "mode"), so CH1-16 are compared with
    what NaviCore reads after the way back, where the same reset has happened. NaviCore's bindings on CH17-24 are
    unbound around it (_hi_inert). rc_ch lists all 24 of NaviCore's channels (rc_telemetry.h:1696-1726; best effort,
    noted when none is relayed). On the way back a 16-locked reader decodes the first SBUS-24 frame's 25-byte prefix
    once (D-NC73, sbus.sbus24_return_no_prefix_decode's); nothing here depends on that frame."""
    ctl, nc, cfg, ncfg = _setup(bench)
    if not ctl.verbs.get("saved24"):
        raise Skip("the controller's saved mode is SBUS-16")
    w1 = usb_wcb(bench)
    found = nc.sbus_dump()["channels"]
    problems, notes = [], []
    with _hi_inert(bench, ncfg) as (g, unbound):
        nc = g.nc if g else nc
        try:
            r = ctl.sbus16(True)
            if r.get("sbus24") is not False or r.get("saved24") is not True:
                problems.append(f"the frame-format verb answered {r}")
            time.sleep(0.5)
            d16 = nc.sbus_full_rate()
            raw16 = nc.sbus_raw()
            mon = nc.monitor(1.0)
            rc = [o["ch"] for o in _relayed(w1, "rc_ch", lambda xs: len(xs) >= 3, 4.0) if isinstance(o.get("ch"), list)]
            saved = ctl.cfg().get("sbus24")
            r = ctl.sbus16(False)
            if r.get("sbus24") is not True:
                problems.append(f"the way back answered {r}")
            time.sleep(0.5)
            d24 = nc.sbus_full_rate()
        finally:
            ctl.clear_faults()
            ctl.axes(0, 0, 0, 0)          # the frame-format change put the sticks at 992
            time.sleep(0.3)
    if d16["variant"] != "SBUS-16" or d16["fps"] < SBUS_FULL_FPS or (d16["lost"], d16["failsafe"]) != ("no", "no"):
        problems.append(f"under SBUS-16 #L09 reads {d16['variant']} fps {d16['fps']} lost={d16['lost']} "
                        f"failsafe={d16['failsafe']}")
    if len(d16["channels"]) != 16:
        problems.append(f"#L09 listed {len(d16['channels'])} channels under SBUS-16")
    if len(raw16) != FRAME_LEN[16] or raw16[0] != HEADER or raw16[-1] != FOOTER or raw16[-2] != 0:
        problems.append(f"#L13 under SBUS-16: {raw16.hex().upper()} ({len(raw16)} bytes)")
    elif decode(raw16)["channels"] != d16["channels"]:
        problems.append("#L13's frame does not decode to #L09's channels")
    if d24["variant"] != "SBUS-24" or d24["fps"] < SBUS_FULL_FPS:
        problems.append(f"back on SBUS-24 #L09 reads {d24['variant']} fps {d24['fps']}")
    elif d24["channels"][:16] != d16["channels"]:
        problems.append(f"CH1-16 under SBUS-16 {d16['channels']} differ from SBUS-24's {d24['channels'][:16]}")
    elif d24["channels"][16:] != found[16:]:
        problems.append(f"CH17-24 did not come back: {d24['channels'][16:]}, were {found[16:]}")
    shapes = {(f["sbus"].get("chCount"), f["sbus"].get("frameLen"), len(f["sbus"].get("channels") or [])) for f in mon}
    if mon and shapes != {(16, 25, 16)}:
        problems.append(f"the monitor's (chCount, frameLen, channels) under SBUS-16: {sorted(shapes)}")
    if not rc:
        notes.append("no rc_ch relayed under SBUS-16 (best effort)")
    else:
        hi = [c[16:24] for c in rc if len(c) == 24]
        if any(h != [SBUS_CENTER] * 8 for h in hi) or len(hi) != len(rc):
            problems.append(f"rc_ch's CH17-24 under SBUS-16: {hi[:3]} (expected eight 992s in 24 values)")
        if rc[-1][:16] != d16["channels"]:
            notes.append(f"the last rc_ch's CH1-16 {rc[-1][:16]} differ from #L09's (read at another moment)")
    if saved is not True:
        problems.append(f"getcfg reported sbus24 {saved} during the test: the frame format was saved")
    notes.append(f"{len(mon)} monitor frames, {len(rc)} rc_ch lines; unbound on CH17-24: {unbound or 'none'}")
    _put_back(ctl, nc, cfg, found, problems)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.sbus24_return_no_prefix_decode", "(should) When the stream goes from SBUS-16 back to SBUS-24, NaviCore's "
      "reader, still locked on 16, never decodes the first 25 bytes of an SBUS-24 frame as a frame of its own: with "
      "byte 24 at 0x00 that prefix passes as an SBUS-16 frame whose flags byte is CH17's low bits",
      needs=["sbus", "navicore"], links=[])
def sbus24_return_no_prefix_decode(bench):
    """NAVICORE.md D-NC73. Locked on SBUS-16, the eager flush decodes a buffer the moment it holds 25 bytes ending in
    0x00 (sbus_reader.h :167-174), and an SBUS-24 frame whose byte 24 is 0x00 (CH17 under 256, CH18 a multiple of 32:
    the bench at rest) does: only the byte after it, not a header, drops the lock (pendingLen16Check_, :134-141), and
    by then the misframed frame has been decoded and handed to processSbus, its flags read from CH17's low byte (0xAD
    on this bench: failsafe and lost) and CH17-24 set to 992. The stream is stopped while the controller goes back to
    SBUS-24, one frame is let through, and #L13 read; the (should) is that NaviCore decoded nothing new or a whole
    frame, never a prefix. CH17/CH18 are set raw when the controller's own values would not make byte 24 zero.
    NaviCore's bindings on CH17-24 are unbound for it (_hi_inert)."""
    ctl, nc, cfg, ncfg = _setup(bench)
    if not ctl.verbs.get("saved24"):
        raise Skip("the controller's saved mode is SBUS-16")
    found = nc.sbus_dump()["channels"]
    problems, notes, bad = [], [], []
    with _hi_inert(bench, ncfg) as (g, unbound):
        nc = g.nc if g else nc
        try:
            ctl.sbus16(True)
            time.sleep(0.5)
            d16 = nc.sbus_full_rate()
            if d16["variant"] != "SBUS-16":
                raise AssertionError(f"NaviCore did not follow the controller to SBUS-16 (#L09: {d16['variant']})")
            f0 = _quiet(ctl, nc)
            raw16 = nc.sbus_raw()
            ctl.sbus16(False)                     # the stream stays off
            if found[16] >= 256 or found[17] % 32:
                ctl.channel(17, found[16] & 0xFF)
                ctl.channel(18, SBUS_CENTER)
                notes.append(f"CH17/CH18 set raw to {found[16] & 0xFF}/{SBUS_CENTER} for byte 24 = 0")
            ctl.stream(True, frames=1)
            time.sleep(0.2)
            f1 = _frames_count(nc)
            raw1 = nc.sbus_raw()
            ctl.stream(True)
            time.sleep(0.6)
            d24 = nc.sbus_full_rate()
            raw24 = nc.sbus_raw()
        finally:
            ctl.clear_faults()
            ctl.axes(0, 0, 0, 0)          # the frame-format change put the sticks at 992
            time.sleep(0.3)
    if d24["variant"] != "SBUS-24" or d24["fps"] < SBUS_FULL_FPS or len(raw24) != FRAME_LEN[24]:
        problems.append(f"NaviCore did not come back to SBUS-24 at full rate ({d24['variant']}, fps {d24['fps']})")
    elif raw24[24] != 0:
        problems.append(f"the SBUS-24 frame's byte 24 is {raw24[24]:#04x}, not 0x00: the prefix could not pass")
    elif raw1 not in (raw16, raw24):
        if raw1 == raw24[:FRAME_LEN[16]]:
            bad.append(f"the one SBUS-24 frame let through made NaviCore, locked on SBUS-16, decode its first 25 "
                       f"bytes as a frame ({f1 - f0} decoded): flags {raw1[-2]:#04x} - byte 23, CH17's low bits - "
                       f"{'a failsafe' if raw1[-2] & FLAG_FAILSAFE else 'no failsafe'}, and CH17-24 set to 992")
        else:
            bad.append(f"the one SBUS-24 frame let through left #L13 holding neither it nor the last SBUS-16 frame "
                       f"({f1 - f0} decoded): {_describe(raw1, raw24)}")
    notes.append(f"unbound on CH17-24: {unbound or 'none'}")
    _put_back(ctl, nc, cfg, found, problems)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)
    assert not bad, "(should, D-NC73) " + "; ".join(bad)


# ============================================================ malformed bursts
@test("sbus.lock_after_glitch", "Malformed bursts between good frames never make NaviCore decode a frame it was not "
      "sent: noise with no header, two frames back to back, a frame with a pause inside it, a frame cut short where "
      "the next frame's bytes cannot close a window on 0x00, and one cut to an SBUS-16 shape - a burst that is not a "
      "whole frame decodes nothing by itself, every frame decoded after it is one the controller sent, and the stream "
      "comes back at full rate (the engine inert: nothing moves)", needs=["sbus", "navicore"], links=[])
def lock_after_glitch(bench):
    """sbus_reader.h: out of a frame only a header byte starts one (:132-150), a locked reader decodes a buffer the
    moment its frame length ends on a footer (:167-174), 40 bytes drop the buffer (:177-181), and a silence parses a
    structurally complete buffer (:187-189). Each case stops the stream, sends one burst (the controller's "glitch"),
    then lets good frames through one at a time, reading #L09's counter and #L13 after the burst and after each
    frame; hil/sbus.py ReaderModel says what each should decode, and the note compares. The cuts are chosen from the
    resting frame by the model: one after which the reader is idle at once, one it recovers from over two frames
    (every frame decoded real), and 25 bytes when byte 24 is 0x00 - a whole SBUS-16 shape, which a silence parses as
    16 and so breaks the 24-lock until three frames in a row: nothing decodes before that. Cuts that close a window
    on 0x00 are sbus.truncated_frame_no_phantom's (D-NC72). The plan expected every glitch to reset the lock streak;
    only a burst that parses as the other variant does (tryParseAndReset's malformed branch, :240-243, is never
    reached: both flushes take only a complete buffer)."""
    ctl, nc, cfg, ncfg = _setup(bench)
    frame = nc.sbus_raw()
    if len(frame) != FRAME_LEN[24] or decode(frame)["flags"]:
        raise Skip(f"#L13 holds no resting SBUS-24 frame with flags 0 ({frame.hex().upper()})")
    phantom, clean, later = _truncations(frame)
    cases = [("garbage", 24, 1), ("double", 2, 1), ("gap", 5, 1)]
    cases += [("truncate", n, 1) for n in [n for n in clean if n != 25][:1]]
    cases += [("truncate", n, 3) for n in sorted(later, key=lambda n: abs(n - 18))[:1]]
    if frame[24] == 0:
        cases.append(("truncate", 25, 3))
    found = nc.sbus_dump()["channels"]
    problems, notes, results = [], [], []
    with _inert(bench) as g:
        nc = g.nc
        try:
            for kind, n, steps in cases:
                results.append((kind, n, _glitch_case(ctl, nc, frame, kind, n, steps)))
                time.sleep(0.6)
            after = nc.sbus_full_rate()
        finally:
            ctl.clear_faults()
    for kind, n, r in results:
        what = f"{kind} {n}"
        counts = [c for c, _ in r["steps"]]
        if kind in ("garbage", "truncate") and counts[0]:
            problems.append(f"{what}: NaviCore decoded {counts[0]} frame(s) from the burst alone")
        for k, (c, raw) in enumerate(r["steps"]):
            if raw != frame:
                problems.append(f"{what}: {'the burst' if not k else f'good frame {k}'} left #L13 holding a frame the "
                                f"controller never sent: {_describe(raw, frame)}")
                break
        if kind == "truncate" and n == FRAME_LEN[16] and any(counts[1:3]):
            problems.append(f"{what}: after a burst that parses as SBUS-16 the next two frames decoded {counts[1:3]}: "
                            f"nothing may decode before three frames of one variant in a row")
        notes.append(f"{what}: NaviCore {counts}, model {r['model']}")
    if after["fps"] < SBUS_FULL_FPS or after["variant"] != "SBUS-24" or (after["lost"], after["failsafe"]) != ("no",
                                                                                                          "no"):
        problems.append(f"after the bursts #L09 reads {after['variant']} fps {after['fps']} lost={after['lost']} "
                        f"failsafe={after['failsafe']}")
    notes.append(f"cuts the model finds decode garbage: {phantom}")
    _put_back(ctl, nc, cfg, found, problems)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.truncated_frame_no_phantom", "(should) A frame cut short never makes NaviCore decode a frame it was not "
      "sent: after a lone header byte, or a frame cut where the next frame's bytes close a 36-byte window on 0x00, and "
      "one good frame, the last frame NaviCore decoded is one the controller sent (the engine inert: nothing moves)",
      needs=["sbus", "navicore"], links=[])
def truncated_frame_no_phantom(bench):
    """NAVICORE.md D-NC72. A partial frame is never dropped: both flushes take only a complete buffer (sbus_reader.h
    :115-117, :187-189), so a cut frame - or a lone 0x0F - stays buffered and the next frame's bytes go on filling it.
    Once locked, the eager flush decodes any 36-byte buffer that ends on 0x00 (:167-174), and the malformed burst never
    broke the lock (the malformed branch of tryParseAndReset, :240-243, is unreachable; the comment at :217-218 says a
    malformed frame breaks the streak). A lone header always does it: the window ends on the next frame's flags byte,
    0x00 from a healthy transmitter. The decoded window is the cut bytes plus the next frame's first ones: shifted
    garbage in every channel, and in the flags byte another channel's bits. The cuts come from the resting frame by
    hil/sbus.py ReaderModel (1 always; 11 on this bench, where byte 24 is 0x00); the stream is stopped, the cut sent,
    one good frame let through, and #L13 read. The (should): the reader never decodes a window that spans a silence."""
    ctl, nc, cfg, ncfg = _setup(bench)
    frame = nc.sbus_raw()
    if len(frame) != FRAME_LEN[24] or decode(frame)["flags"]:
        raise Skip(f"#L13 holds no resting SBUS-24 frame with flags 0 ({frame.hex().upper()})")
    phantom, _, _ = _truncations(frame)
    if not phantom:
        raise Skip("the model finds no cut of this frame that closes a window on 0x00")
    found = nc.sbus_dump()["channels"]
    problems, bad, results = [], [], []
    with _inert(bench) as g:
        nc = g.nc
        try:
            for n in phantom[:3]:
                results.append((n, _glitch_case(ctl, nc, frame, "truncate", n, 1)))
                time.sleep(0.6)
        finally:
            ctl.clear_faults()
    for n, r in results:
        (c0, raw0), (c1, raw1) = r["steps"]
        if c0 or raw0 != frame:
            problems.append(f"the first {n} byte(s) of a frame decoded {c0} frame(s) by themselves (#L13 "
                            f"{_describe(raw0, frame)})")
        elif raw1 != frame:
            window = frame[:n] + frame[:FRAME_LEN[24] - n]
            how = "the cut bytes and the next frame's first " + str(FRAME_LEN[24] - n) if raw1 == window else "a frame"
            bad.append(f"after the first {n} byte(s) of a frame and one good frame NaviCore decoded {how} "
                       f"({_describe(raw1, frame)}; the model predicted it: {r['model'] == [0, 1] and raw1 == window})")
    _put_back(ctl, nc, cfg, found, problems)
    bench.note(f"cuts tried: {[n for n, _ in results]}")
    assert not problems, "; ".join(problems)
    assert not bad, "(should, D-NC72) " + "; ".join(bad)


@test("sbus.prefix_ambiguity_raw", "With CH17 set raw to each value under 256 whose low byte reads as flags (0x04, 0x08, "
      "0x0C, 0xFF and the bench's own) and CH18 a multiple of 32, SBUS-24 frame byte 24 is 0x00 and byte 23 carries "
      "those bits: under load NaviCore still reads SBUS-24 with no failsafe or lost flag, CH17 exact, and a matrix "
      "press still dispatches", needs=["sbus", "navicore"], links=[])
def prefix_ambiguity_raw(bench):
    """The raw form of sbus.prefix_ambiguity_ch17: an SBUS-24 frame's first 25 bytes are a whole SBUS-16 frame whenever
    byte 24 (CH17's top three bits, CH18's low five) is 0x00, and read that way byte 23 - CH17's low byte - would be
    its flags. The reader guards it twice: a 25-byte gap flush waits for the next header (sbus_reader.h :115-117), and
    locked on 24 the eager flush waits for 36 bytes (:167-169). CH17 goes raw to each value; CH18 stays where it is,
    which must be a multiple of 32 (992 here). NaviCore's bindings on CH17 must read every value alike (a switch in its
    low band, no knob), and the controller switch that owns CH17 puts it back. Load as in sbus.lock_under_load; one
    press of an unmapped matrix button halfway (rc_trig only)."""
    ctl, nc, cfg, ncfg = _setup(bench)
    frame = nc.sbus_raw()
    found = nc.sbus_dump()["channels"]
    if len(frame) != FRAME_LEN[24]:
        raise Skip("NaviCore holds no SBUS-24 frame")
    if found[17] % 32:
        raise Skip(f"CH18 reads {found[17]}, not a multiple of 32: byte 24 cannot be 0x00 without moving it")
    k17 = next((k for k, s in enumerate(cfg.get("sw") or []) if s.get("c") == 17), None)
    if k17 is None:
        raise Skip("no controller switch owns CH17 to put it back")
    if 17 in (ncfg.get("matrixChannel"),) or any(k.get("channel") == 17 and k.get("function")
                                                   for k in (ncfg.get("knobs") or {}).values()):
        raise Skip("NaviCore reads CH17 as its matrix or a knob: a raw value there would act")
    moved = [lab for lab, s in (ncfg.get("switches") or {}).items() if s.get("channel") == 17
             and _switch_pos(found[16], s.get("positions", 3)) != 0]
    if moved:
        raise Skip(f"CH17 rests at {found[16]}, outside the low band of NaviCore's switch {moved}: the raw values, all "
                   f"under 256, would move it")
    mode = nc.mode()
    (ib, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    values = [4, 8, 12, 255] + ([found[16]] if found[16] < 256 and found[16] not in (4, 8, 12, 255) else [])
    slots = nc.local_slots(ncfg)
    base = nc.sbus_dump()
    samples, problems, pressed = [], [], None
    m = nc.dev.mark()
    try:
        nc.set_debug_flags(FLAGS_ALL)
        nc.ack({"type": "START_MONITOR"})
        for k, v in enumerate(values):
            ctl.channel(17, v)
            time.sleep(0.3)
            raw = nc.sbus_raw()
            if len(raw) != FRAME_LEN[24] or raw[23] != v or raw[24] != 0:
                problems.append(f"CH17 = {v}: frame bytes 23-24 read {raw[23:25].hex().upper()}, not {v:02X}00")
            t_end = time.monotonic() + 3.0
            while time.monotonic() < t_end:
                for c in range(2):
                    if slots:
                        nc.cli(f"?MAE,GET,{slots[0][0]},{c}")
                s = nc.sbus_dump()
                samples.append(s)
                if len(s["channels"]) < 17 or s["channels"][16] != v:
                    problems.append(f"CH17 read {s['channels'][16:17]}, not {v}")
                time.sleep(0.3)
            if pressed is None and k == len(values) // 2:
                pressed = nc.dev.mark()
                _press(ctl, ib, 0.12)
    finally:
        ctl.button(ib, False)
        nc.ack({"type": "STOP_MONITOR"})
        nc.set_debug_flags(0)
        ctl.switch(k17, (cfg["sw"][k17]).get("pos", 0))
        time.sleep(0.5)
    problems += _samples_ok(samples, base)
    bad = [f["sbus"] for f in _monitor_frames(nc.dev.since(m)) if f["sbus"].get("failsafe") or f["sbus"].get("lost")]
    if bad:
        problems.append(f"{len(bad)} monitor frames flag failsafe or a lost frame, first {bad[0]}")
    if pressed is None or not _taps(nc, pressed, mode, slot):
        problems.append("the matrix press made under load did not dispatch")
    _put_back(ctl, nc, cfg, found, problems)
    bench.note(f"CH17 values {values}; {len(samples)} samples; CH18 {found[17]}")
    assert not problems, "; ".join(sorted(set(problems))[:10])


# ============================================================ one frame
@test("sbus.one_frame_dip", "One frame at neutral in the middle of a held matrix press: at matrixDebounceFrames 1 it is "
      "a release and a second press (tap 2), at 2 it changes nothing (tap 1) - the release debounce (unmapped slot: "
      "rc_trig only)", needs=["sbus", "navicore"], links=[])
def one_frame_dip(bench):
    """processSbus confirms a release only after matrixDebounceFrames neutral frames (NaviCore.ino:2825-2847): at 1 a
    single neutral frame re-arms the matrix and ends the hold (rcMatrixRelease :2379-2402), so the in-band frames after
    it commit a second press of the same button inside the tap window (:2853-2856; RCRadio_Matrix_Buttons :2326-2336);
    at 2 the lone neutral frame only resets the candidate, and the matrix stays disarmed until the real release. The
    dip is the controller's "glitch" "dip" on the matrix channel, one frame at 992 with the button held around it.
    matrixDebounceFrames is set inside nc_guard (1 is the bench's own)."""
    ctl, nc, cfg, ncfg = _setup(bench)
    found = nc.sbus_dump()["channels"]
    mode = nc.mode()
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    mc = ncfg.get("matrixChannel")
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        tap_s = g.before.get("tapWindowMs", 500) / 1000
        try:
            for deb, want in ((1, [2]), (2, [1])):
                if deb != 1 or g.before.get("matrixDebounceFrames") != 1:
                    nc.set_config({"matrixDebounceFrames": deb})
                    time.sleep(0.5)
                nm = nc.dev.mark()
                ctl.button(i, True)
                time.sleep(0.2)
                ctl.glitch("dip", mc)
                time.sleep(0.2)
                ctl.button(i, False)
                time.sleep(tap_s + 1.0)
                got = [t for _, t in _taps(nc, nm, mode, slot)]
                notes.append(f"debounce {deb}: taps {got}")
                if got != want:
                    problems.append(f"at matrixDebounceFrames {deb} a press with one neutral frame inside it gave taps "
                                    f"{got}, expected {want}")
        finally:
            ctl.button(i, False)
            ctl.clear_faults()
            time.sleep(tap_s + 0.3)
    _put_back(ctl, nc, cfg, found, problems)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ RAM only
def _reset_controller(bench, ctl):
    """Reset the controller through its USB-Serial/JTAG port (SbusCtl.reset_rts) and reopen its port -> (a new SbusCtl on
    it, the lines its boot printed). The port write itself can hang tens of seconds while the chip's USB re-enumerates
    (docs/HIL_TESTING.md §6); the reset is proven by the boot record (count up, or an uptime shorter than the time
    since the pulse), as sbus.signal_loss_controller_reset proves it."""
    b0 = ctl.bootlog()
    t_pulse = time.monotonic()
    ctl.reset_rts()
    time.sleep(max(0.0, t_pulse + 4 - time.monotonic()))
    bench.close_device("sbus")
    reopened, deadline = None, time.monotonic() + 60
    while reopened is None and time.monotonic() < deadline:
        try:
            reopened = bench.dev("sbus")
        except Exception:  # noqa: BLE001 - the port comes back once Windows has re-enumerated it
            time.sleep(2)
    if reopened is None:
        raise AssertionError("the SBUS controller's port did not come back within 60 s")
    new = SbusCtl(reopened)
    new.ping()
    b1 = new.bootlog(tries=2)
    if not (b1["n"] > b0["n"] or b1["up"] < (time.monotonic() - t_pulse) * 1000):
        raise AssertionError(f"the controller did not reset: boot count {b0['n']}->{b1['n']}, up {b1['up']} ms")
    return new, reopened.since(0)


@test("sbus.test_verbs_ram_only", "OPT-IN (sbus_reset): the INF8 test verbs save nothing - with SBUS-16, a raw value on "
      "the rx stick's channel and the lost-frame flag all set, a controller reset brings back SBUS-24 (the saved mode), "
      "flags 0, the stream on and the channel at its boot value 992", needs=["sbus", "navicore"], links=[],
      opt_in="sbus_reset")
def test_verbs_ram_only(bench):
    """The one hazard in INF8 is the frame-format verb, which saves without "save":false. Nothing the verbs set is
    saved (SBUSController.ino: the HIL test state's globals start at their production values; "mode" with "save":false
    leaves cfg.sbus24), so a reset clears it all, the frame format included. The controller must be in its boot state,
    as for sbus.signal_loss_controller_reset, since the reset puts every control back to it; its boot sets every
    channel to 992 before the controls re-apply (initRuntimeState), and the sticks are centred again afterwards.
    NaviCore's bindings on CH17-24 are unbound around the SBUS-16 part (_hi_inert)."""
    ctl, nc, cfg, ncfg = _setup(bench)
    if (any(s.get("pos") != s.get("d") for s in cfg.get("sw", [])) or any(s.get("pct") != 50 for s in cfg.get("sl", []))
            or any(x.get("cur") != SBUS_CENTER for x in cfg.get("tr", []))):
        raise Skip("the controller is not in its boot state; a reset would move channels")
    if not ctl.verbs.get("saved24"):
        raise Skip("the controller's saved mode is SBUS-16")
    ch = _stick(nc, cfg, ncfg, "rx")
    found = nc.sbus_dump()["channels"]
    problems, notes, boot, st = [], [], [], None
    with _hi_inert(bench, ncfg) as (g, unbound):
        nc = g.nc if g else nc
        try:
            ctl.sbus16(True)
            ctl.channel(ch, 700)
            ctl.flags(FLAG_LOST)
            time.sleep(0.5)
            d = nc.sbus_full_rate()
            if d["variant"] != "SBUS-16" or d["lost"] != "YES" or d["channels"][ch - 1] != 700:
                raise AssertionError(f"the verbs did not take before the reset: {d['variant']}, lost={d['lost']}, "
                                     f"CH{ch} = {d['channels'][ch - 1]}")
            ctl, boot = _reset_controller(bench, ctl)
            st = ctl.test_state()
            time.sleep(2.0)
            d = nc.sbus_full_rate()
        finally:
            try:
                ctl.clear_faults()
                ctl.axes(0, 0, 0, 0)      # the boot, or the frame-format change, put the sticks at 992
            except Exception as e:  # noqa: BLE001 - a failed reset closed the old port; its own error is the one
                notes.append(f"clean-up after the reset failed: {e}")
            time.sleep(0.3)
    want = {"flags": 0, "stream": True, "budget": -1, "sbus24": True, "saved24": True}
    if st is None or {k: st.get(k) for k in want} != want:
        problems.append(f"after the reset the test state reads {st}, not {want}")
    if d["variant"] != "SBUS-24" or d["fps"] < SBUS_FULL_FPS or (d["lost"], d["failsafe"]) != ("no", "no"):
        problems.append(f"after the reset NaviCore reads {d['variant']} fps {d['fps']} lost={d['lost']}")
    elif d["channels"][ch - 1] != SBUS_CENTER:
        problems.append(f"after the reset CH{ch} reads {d['channels'][ch - 1]}, not its boot value {SBUS_CENTER}")
    if any("WiFi section missing from config" in x for x in boot):
        problems.append("the controller rewrote its config on boot: report it")
    notes.append(f"unbound on CH17-24: {unbound or 'none'}")
    _put_back(ctl, nc, cfg, found, problems)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)
