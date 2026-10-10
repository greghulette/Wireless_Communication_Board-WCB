"""NaviCore's RC engine driven the way a pilot drives it, through SBUS from the bench controller: the button matrix and
its taps, switch tiers, the mode switch, knobs, and the SBUS reader under load and after stalls
(docs/hil_plan/NAVICORE.md NC-WP5, ids sbus.*; the USB-driven half is s41, whose helpers these tests share). Every test
but sbus.lock_under_load (it only reads) moves an SBUS channel or can make NaviCore move a servo, and those are in
hil/servos.py.

The inputs. The controller (hil/sbus.py SbusCtl) is driven with JSON only. Its rx and ry sticks (CH1 and CH2 on this
bench: getcfg rx/ry) are channels NaviCore binds to nothing (NAVICORE.md §1.4; _stick re-checks against the running
config), steerable to the exact SBUS count: axisToSbusRange is inverted here (axis_arg), and #L09 confirms the count
where precision matters. A test binds a knob, a switch or the mode function to a stick inside nc_guard, so the
rebinding is undone byte-identically. The matrix is exercised with the controller's own matrix buttons: RB1 and RB2
decode to slots no mapping uses in any mode here (rc_trig only, or a guarded marker mapping), and the controller's lua
buttons sit on values no band covers, where a test places a logical band (22-36). Everything moved is released: the
sticks are centred and the buttons let go in `finally`, and a mode changed over the mesh is set back (the guard also
does).

The outputs. A knob's passthrough goes to a remote Maestro slot nobody hosts (slot 4, device 4): its Pololu frames
reach W1's S1 probe through W1's Maestro_Remote forward and move nothing (NAVICORE.md §1.2). The Kyber broadcast that
carries them is best effort, so a count on W1 S1 is at most what NaviCore sent; on a NAVICORE_HIL_HOOKS image DBG_WIRE
shows what it wrote, quoted when a frame is missing. Switch tiers and mappings fire ';S2HIL...' markers at W1's S2
probe (s41 _act). rc_trig lines on NaviCore's USB give the tap engine's verdict and, on the host clock, its timing
(each is emitted when the gesture resolves, NaviCore.ino:2225-2285).

What moves for real: the mode-aware knobs J2 (NaviCore's Maestro 1, the dome) and J4 (Maestro 2 on W2, and the remote
slots) snap to their stick on every mode change (resetModeAwareKnobs, NaviCore.ino:2709-2720), so the tests that change
the mode move them; every SBUS channel moved is re-emitted on SBUS OUT, and the controller's own RC PWM 1-2 follow
CH1-2.
"""
import json
import random
import re
import time

from hil.nc_guard import nc_guard
from hil.navicore import DBG_HCR, SBUS_FULL_FPS
from hil.ncmesh import bridged
from hil.runner import Skip, test
from hil.sbus import SBUS_CENTER, band, decode
from suites.common import link, marker, usb_wcb
from suites.s21_navicore_sbus import _sbus_setup
from suites.s40_navicore_config import KNOBS, SWITCHES, _flushed, _free_profile, _hooks
from suites.s41_navicore_engine import (FLAGS_ALL, _act, _count, _engine_inert, _host_time, _pololu, _probe_ms,
                                        _rc_trigs, _remote_slot, _wait_all, _wire_bytes)

AXIS_INDEX = {"rx": 0, "ry": 1, "ly": 2, "lx": 3}   # axisMin/axisMax/axisReverse index (SBUSController.ino:1103-1116)
NEGATED = ("ry", "ly")                              # ry = -doc.ry, ly = -doc.ly (:1105-1106)
KF_PASSTHROUGH, KF_HCR_VOLUME = 1, 2                # RcKnob.function (processKnobs, NaviCore.ino:2598-2700)
CMD_TARGET, CMD_SPEED, CMD_ACCEL = 0x04, 0x07, 0x09  # Pololu commands as they appear after AA <device>
DBG_WIRE = 0x80                                     # hook builds only (navicore_hil.h); another image ignores it
MODE_VALUE = {1: 400, 2: SBUS_CENTER, 3: 1600}      # a stick value inside each mode's band (NaviCore.ino:2802)


# ------------------------------------------------------------------ the sticks
def axis_arg(cfg, axis, value):
    """The {"t":"a"} argument that puts SBUS `value` on the axis's channel: axisToSbusRange (SBUSController.ino:967-971,
    int((v*0.5+0.5)*(max-min)+min+0.5)) inverted, with the sign flips at :1105-1111. +0.1 of a count keeps float
    rounding from landing one low. Skip for a value outside the axis's calibrated range (getcfg aMin/aMax)."""
    i = AXIS_INDEX[axis]
    mn, mx, rev = cfg["aMin"][i], cfg["aMax"][i], bool(cfg["aRev"][i])
    if not min(mn, mx) <= value <= max(mn, mx):
        raise Skip(f"the {axis} stick reaches {min(mn, mx)}-{max(mn, mx)}, not {value}")
    f = max(-1.0, min(1.0, 2.0 * (value + 0.1 - mn) / (mx - mn) - 1.0))
    d = -f if rev else f
    return round(-d if axis in NEGATED else d, 6)


def axis_value(cfg, axis, d):
    """The SBUS value the controller sends for {"t":"a"} argument `d` on `axis` (axisToSbusRange)."""
    i = AXIS_INDEX[axis]
    mn, mx, rev = cfg["aMin"][i], cfg["aMax"][i], bool(cfg["aRev"][i])
    f = -d if axis in NEGATED else d
    f = max(-1.0, min(1.0, -f if rev else f))
    return max(0, min(2047, int((f * 0.5 + 0.5) * (mx - mn) + mn + 0.5)))


class Sticks:
    """The controller's rx and ry sticks, each held at an exact SBUS value; lx and ly stay centred ({"t":"a"} sets all
    four at once, SBUSController.ino:1099-1116)."""

    def __init__(self, ctl, cfg):
        self.ctl, self.cfg, self.arg = ctl, cfg, {"rx": 0.0, "ry": 0.0}

    def set(self, axis, value):
        """Put `value` on the axis's channel -> the host time just after the command went out."""
        self.arg[axis] = axis_arg(self.cfg, axis, value)
        self.ctl.axes(0, 0, self.arg["rx"], self.arg["ry"])
        return time.monotonic()

    def center(self):
        self.arg = {"rx": 0.0, "ry": 0.0}
        self.ctl.axes(0, 0, 0, 0)


def _bound_channels(ncfg):
    """SBUS channels NaviCore's config reads to act on: the matrix, every switch (tiers, the mode function, a knob's mode
    override), every knob with a function."""
    out = {ncfg.get("matrixChannel")}
    out |= {s.get("channel") for s in (ncfg.get("switches") or {}).values()}
    out |= {k.get("channel") for k in (ncfg.get("knobs") or {}).values() if k.get("function")}
    out.discard(None)
    out.discard(0)
    return out


def _stick(nc, cfg, ncfg, axis="rx"):
    """The SBUS channel of the controller's `axis` stick, or Skip: NaviCore's running config must bind it to nothing,
    and it must rest at its centre now."""
    ch = cfg.get(axis)
    if not ch or not 1 <= ch <= 24:
        raise Skip(f"the controller names no channel for its {axis} stick")
    if ch in _bound_channels(ncfg):
        raise Skip(f"NaviCore binds CH{ch} (the {axis} stick) to something: moving it could act")
    rest = axis_value(cfg, axis, 0.0)
    now = nc.sbus_dump()["channels"][ch - 1]
    if now != rest:
        raise Skip(f"CH{ch} reads {now}, not the {axis} stick's centre {rest}: someone left it deflected")
    return ch


def _check_value(nc, ch, want, what):
    """[] when #L09 shows `want` on CH<ch>, else one problem (the stick did not land on its count)."""
    got = nc.sbus_dump()["channels"][ch - 1]
    return [] if got == want else [f"{what}: CH{ch} reads {got}, not {want} (the controller's axis mapping?)"]


# ------------------------------------------------------------------ what NaviCore's config holds
def _free_knobs(before):
    """Knob labels that are disabled and drive nothing (channel 0, function 0, no outputs, not mode-aware), in order."""
    out = []
    for label in KNOBS:
        k = (before.get("knobs") or {}).get(label) or {}
        if k.get("channel") == 0 and k.get("function") == 0 and not k.get("outputs") and not k.get("modeAware"):
            out.append(label)
    return out


def _free_switches(before):
    """[(index, label)] of switches with no tiers or notes that are neither the mode function nor a knob's mode override:
    rebinding one fires nothing it did not fire before."""
    mode_sw = (before.get("funcBindings") or {}).get("mode")
    overrides = {k.get("modeSwitchOverride") for k in (before.get("knobs") or {}).values()}
    out = []
    for i, label in enumerate(SWITCHES):
        s = (before.get("switches") or {}).get(label) or {}
        if i == mode_sw or i in overrides or any(re.fullmatch(r"p[0-2](note)?", k) for k in s):
            continue
        out.append((i, label))
    return out


def _knob(channel, function=KF_PASSTHROUGH, outputs=(), **kw):
    """A whole knob object for SET_CONFIG: a knob named there takes its default for every key it omits
    (rc_config.h:1645-1668)."""
    k = {"channel": channel, "function": function, "reverse": False, "modeAware": False, "modeSwitchOverride": -1,
         "outputs": list(outputs)}
    k.update(kw)
    return k


def _out(ch, lo=4000, hi=8000, target=4, **kw):
    o = {"target": target, "maestroCh": ch, "posMin": lo, "posMax": hi}
    o.update(kw)
    return o


def _free_channels(before, slot, n):
    """`n` channels of Maestro `slot` that no knob output in `before` drives, from 5 up (J4 drives ch 0 of slots 2-7)."""
    used = {o.get("maestroCh") for k in (before.get("knobs") or {}).values()
            for key in ("outputs", "outputs2", "outputs3") for o in (k.get(key) or []) if o.get("target") == slot}
    chans = [c for c in range(5, 24) if c not in used]
    if len(chans) < n:
        raise Skip(f"fewer than {n} free channels on Maestro slot {slot}")
    return chans[:n]


def _cdiv(a, b):
    """C integer division (truncates toward zero), as the firmware's long arithmetic does."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def knob_pos(v, lo, hi, reverse=False, mid_closed=False):
    """What processKnobs sends for SBUS value `v` on an output posMin `lo`, posMax `hi`: the reverse flip (1983 - v,
    NaviCore.ino:2642-2648), then sbusToRange or sbusToRangeMidClosed (:593-627), clamped to the endpoints."""
    raw = max(0, min(2047, 1983 - v)) if reverse else v
    if mid_closed:
        center = (172 + 1811) // 2
        if raw <= center:
            return lo
        mapped = _cdiv((raw - center) * (hi - lo), 1811 - center) + lo
    else:
        mapped = _cdiv((raw - 172) * (hi - lo), 1811 - 172) + lo
    return max(min(lo, hi), min(max(lo, hi), mapped))


# ------------------------------------------------------------------ Pololu frames on W1 S1
PAYLOAD = {0x04: 3, 0x07: 3, 0x09: 3, 0x10: 1, 0x13: 0, 0x21: 0, 0x22: 0, 0x24: 0, 0x27: 1, 0x28: 3}


def pololu_frames(data):
    """[(device, cmd, ch, value)] of the Pololu-protocol frames in `data` (AA <dev> <cmd> <payload>): cmd the 7-bit
    command, value the 14-bit number of a target/speed/accel frame (None for the others). A byte that starts no known
    frame is skipped; no data byte can be 0xAA (they are all below 0x80)."""
    out, i = [], 0
    while i + 2 < len(data):
        if data[i] != 0xAA or data[i + 2] not in PAYLOAD:
            i += 1
            continue
        dev, cmd, n = data[i + 1], data[i + 2], PAYLOAD[data[i + 2]]
        if i + 3 + n > len(data):
            break
        body = data[i + 3:i + 3 + n]
        if n == 3:
            out.append((dev, cmd, body[0], body[1] | (body[2] << 7)))
        else:
            out.append((dev, cmd, body[0] if n else None, None))
        i += 3 + n
    return out


def _frames(l11, since, dev, chans):
    """[(cmd, ch, value)] of the frames for Maestro `dev` on channels `chans` that W1 S1 received since `since`."""
    return [(c, ch, v) for d, c, ch, v in pololu_frames(l11.received(since)) if d == dev and ch in chans]


def _wait_frames(l11, since, dev, chans, n, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        got = _frames(l11, since, dev, chans)
        if len(got) >= n or time.monotonic() >= deadline:
            return got
        time.sleep(0.05)


def _witnesses(bench):
    """The probe wires on the other boards that forward NaviCore's remote Maestro stream: W2 S1 (Maestro_Remote) and
    W3 S2 (W3's Kyber Maestro port, ?KYBER,LOCAL); missing ones are left out."""
    return [bench.links.get(2, "S1"), bench.links.get(3, "S2")]


def _missed_only(got, want):
    """`got` is `want` with one or more frames left out and nothing else changed."""
    it = iter(want)
    return len(got) < len(want) and all(any(f == w for w in it) for f in got)


class Watch11:
    """W1 S1's Pololu frames for one Maestro device and a set of its channels, with NaviCore's own DBG_WIRE copy quoted
    on a hook image when they disagree. step(action, want) marks, runs the action and checks that exactly `want`
    follows, in order (none, for an empty `want`, within `quiet` s).

    The remote Maestro stream is an unacknowledged ESP-NOW broadcast (NaviCore wcb_config.h), so a receiver can miss a
    frame no one resends: W1 S1 lost the 'reverse, 1200' frame of sbus.knob_passthrough_remote while W3 forwarded it
    (run 20261009-182550; 0 of 135 lost in a bench measurement after), and W1 and W2 both lost a save's frame W3
    forwarded (run 20261010-000816). With `witnesses` - the other boards that forward the stream: the W2 S1 tap (the
    other Maestro_Remote board, which s44's Wires reads too) and W3's Kyber Maestro-port tap (W3 S2) - a step where W1
    S1 only missed frames a witness got exactly is noted in `lost`, not failed: NaviCore sent the right frame. A step
    can't be sent again as s44 does; a stick already at its value sends nothing. More than one such step in a test is a
    problem (lost_problem). Each step expects NaviCore's settle resend as well (step())."""

    def __init__(self, l11, nc, dev, chans, wire, witnesses=()):
        self.l11, self.nc, self.dev, self.chans, self.wire = l11, nc, dev, set(chans), wire
        self.witnesses = [w for w in witnesses if w is not None]
        for w in self.witnesses:
            w.listen()
        self.problems, self.lost = [], []

    def lost_problem(self):
        """[] or the one problem a test with more than one W1-only lost broadcast has."""
        return [f"W1 S1 missed broadcasts a witness received in {len(self.lost)} steps: "
                + "; ".join(self.lost)] if len(self.lost) > 1 else []

    def step(self, action, want, where, quiet=0.6, timeout=2.5):
        # NaviCore's settle resend (navicore-fix9, KNOB_SETTLE_RESEND_MS): every setTarget a knob sends to this remote
        # slot goes out once more when the knob has been still 250 ms, in the same order; speed and accel do not.
        want = list(want) + [f for f in want if f[0] == CMD_TARGET]
        m11, nm = self.l11.mark(), self.nc.dev.mark()
        mws = [w.mark() for w in self.witnesses]
        action()
        if want:
            _wait_frames(self.l11, m11, self.dev, self.chans, len(want), timeout)
        time.sleep(quiet)
        got = _frames(self.l11, m11, self.dev, self.chans)
        if got != list(want) and _missed_only(got, want):
            heard = [w.key for w, m in zip(self.witnesses, mws) if _frames(w, m, self.dev, self.chans) == list(want)]
            if heard:
                self.lost.append(f"{where}: W1 S1 got {got}, {' and '.join(heard)} got all of {list(want)}")
                return got
        if got != list(want):
            note = ""
            if self.wire:
                wrote = [(c, ch, v) for d, c, ch, v in pololu_frames(_wire_bytes(_flushed(self.nc, nm, 0.05)))
                         if d == self.dev and ch in self.chans]
                note = f" (NaviCore's [WIRE] log shows it wrote {wrote})"
            self.problems.append(f"{where}: W1 S1 got {got}, expected {list(want)}{note}")
        return got


def _knob_rig(bench, n_chans=1):
    """(ctl, nc, cfg, ncfg, sticks, rx channel, W1 S1 link, remote slot, its device, free channels on it, hook image?)
    for the knob tests."""
    l11 = link(bench, 1, "S1")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    slot, dev = _remote_slot(ncfg)
    chans = _free_channels(ncfg, slot, n_chans)
    return ctl, nc, cfg, ncfg, Sticks(ctl, cfg), ch, l11, slot, dev, chans, _hooks(nc)


# ------------------------------------------------------------------ the matrix
def _unmapped_buttons(nc, cfg, ncfg, modes, n):
    """[(index, value, slot)] of `n` controller buttons ('btn') on NaviCore's matrix channel whose values decode to
    distinct slots with no mapping in any of `modes`; the matrix channel's resting value and the release value 992
    must decode to nothing. Skip otherwise."""
    mc = ncfg.get("matrixChannel")
    rest = nc.sbus_dump()["channels"][mc - 1]
    if band(ncfg, rest) or band(ncfg, SBUS_CENTER):
        raise Skip("the matrix channel's resting or release value decodes to a slot")
    maps, out = ncfg.get("mappings") or {}, []
    for i, b in enumerate(cfg.get("btn") or []):
        slot = band(ncfg, b.get("v", 0)) if b.get("c") == mc else None
        if slot and slot not in [s for _, _, s in out] and all(str(m * 100 + slot) not in maps for m in modes):
            out.append((i, b["v"], slot))
            if len(out) == n:
                return out
    raise Skip(f"fewer than {n} controller buttons decode to slots unmapped in modes {list(modes)}")


def _press(ctl, i, hold_s):
    """Press button `i` for `hold_s` -> (host time after the press went out, after the release went out)."""
    ctl.button(i, True)
    t0 = time.monotonic()
    time.sleep(hold_s)
    ctl.button(i, False)
    return t0, time.monotonic()


def _taps(nc, since, mode, slot):
    """[(host time, tap number)] of the rc_trig lines for matrix `slot` in `mode` since mark `since`: the tap alone,
    where s41 _rc_trigs keeps (mode, btn, tap)."""
    return [(ts, k[2]) for ts, k in _rc_trigs(nc, since, {(mode, slot)})]



FPS_WINDOW_CLEAR_S = 2.2   # long enough that #L09's fps window (the last whole second loop() timed) excludes an earlier stall

@test("sbus.matrix_logical_band", "A logical band (slots 22-36) decodes like a physical one: a controller lua button "
      "whose value no band covers presses the logical slot once a band is placed on it; where two bands overlap the "
      "lower slot wins; each press gives one rc_trig and its mapping's marker", needs=["sbus", "navicore", "wcb1"],
      links=["W1S2"])
def matrix_logical_band(bench):
    """pwmToButton returns the FIRST band (lowest slot) holding the value and skips 0/0 bands (NaviCore.ino:580-591);
    slots 22-36 are the tool's 'Logical' bands, 0/0 on this bench (NAVICORE.md §1.4); thresholds is written whole
    (rc_config.h:1584-1595). The controller's lua buttons sit on the matrix channel between the physical bands (getcfg
    'lua': 274, 376, ...): two of them, v1 < v2, get band L = v1 +/- 12 and band L2 = v1 - 6 .. v2 + 12, so v1 lies in
    both (L wins) and v2 in L2 alone. Each slot gets a guarded marker mapping in the current mode. #L12 while the button
    is held names the decoded slot (pwmToButton of the live value, NaviCore.ino:3639-3640)."""
    l12 = link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mc, tap_ms = ncfg.get("matrixChannel"), ncfg.get("tapWindowMs", 500)
    vals = sorted({b.get("v") for b in cfg.get("lua") or [] if b.get("c") == mc and b.get("v")
                   and not band(ncfg, b["v"]) and b["v"] < SBUS_CENTER - 100})
    pair = next(((a, b) for a in vals for b in vals if 40 <= b - a <= 250), None)
    if pair is None:
        raise Skip("no two controller lua buttons on the matrix channel sit 40-250 counts apart where no band lies")
    v1, v2 = pair
    i1 = next(i for i, b in enumerate(cfg["lua"]) if b.get("c") == mc and b.get("v") == v1)
    i2 = next(i for i, b in enumerate(cfg["lua"]) if b.get("c") == mc and b.get("v") == v2)
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        mode, maps = nc.mode(), g.before.get("mappings") or {}
        th = g.before.get("thresholds") or []
        free = [s for s in range(22, 37) if s <= len(th) and (th[s - 1].get("minPwm"), th[s - 1].get("maxPwm")) == (0, 0)
                and str(mode * 100 + s) not in maps]
        if len(free) < 2:
            raise Skip(f"fewer than two logical slots are 0/0 and unmapped in mode {mode}")
        L, L2 = free[0], free[1]
        bands = [dict(t) for t in th]
        bands[L - 1].update(minPwm=v1 - 12, maxPwm=v1 + 12)
        bands[L2 - 1].update(minPwm=v1 - 6, maxPwm=v2 + 12)
        planned = {"thresholds": bands}
        if (band(planned, v1), band(planned, v2), band(planned, SBUS_CENTER)) != (L, L2, None):
            raise Skip(f"the bands would not decode as planned: {band(planned, v1)}, {band(planned, v2)}")
        tags = {L: marker("LB"), L2: marker("LC")}
        nc.set_config({"thresholds": bands, "mappings": {str(mode * 100 + s): {"exclusive": False,
                                                                             "t1": [_act(tags[s])]} for s in (L, L2)}})
        time.sleep(0.5)
        try:
            for i, v, slot in ((i1, v1, L), (i2, v2, L2)):
                pm, nm = l12.mark(), nc.dev.mark()
                ctl.lua(i, True)
                time.sleep(0.15)
                held = nc.cli("#L12", r"Mode=\d+", flush=False)
                ctl.lua(i, False)
                time.sleep(tap_ms / 1000 + 1.2)
                trigs = [k for _, k in _rc_trigs(nc, nm)]
                got = l12.received(pm)
                if f"Mode={mode}  matrixBtn={slot}  matrixVal={v}" not in held:
                    problems.append(f"value {v}: while held NaviCore said {held}")
                if trigs != [(mode, slot, 1)]:
                    problems.append(f"value {v}: rc_trig {trigs}, expected [({mode}, {slot}, 1)]")
                other = L2 if slot == L else L
                if _count(got, tags[slot]) != 1 or _count(got, tags[other]):
                    problems.append(f"value {v}: slot {slot}'s marker x{_count(got, tags[slot])}, slot {other}'s "
                                    f"x{_count(got, tags[other])} on W1 S2")
        finally:
            ctl.lua(i1, False)
            ctl.lua(i2, False)
            time.sleep(tap_ms / 1000 + 0.5)
    bench.note(f"lua values {v1}/{v2} on logical slots {L}/{L2} (overlap at {v1}) in mode {mode}")
    assert not problems, "; ".join(problems)


def _blips(ctl, nc, i, mode, slot, tries, tap_ms, max_ms=12.0):
    """`tries` presses of button `i` of about 4 ms on the host, each followed by the tap window -> (commits among the
    valid tries, valid tries, the host-held times). A try whose press ran past max_ms (the host was descheduled between
    the two writes) is not counted."""
    commits = valid = 0
    held = []
    for _ in range(tries):
        nm = nc.dev.mark()
        ctl.button(i, True)
        t0 = time.monotonic()
        time.sleep(0.004)
        t1 = time.monotonic()
        ctl.button(i, False)
        ms = (t1 - t0) * 1000
        held.append(round(ms, 1))
        time.sleep(tap_ms / 1000 + 0.35)
        if ms <= max_ms:
            valid += 1
            commits += bool(_taps(nc, nm, mode, slot))
    return commits, valid, held


def _gesture(ctl, nc, i, mode, slot, presses, tap_ms, hold_s=0.1, gap_s=0.15):
    """`presses` presses of button `i` -> the rc_trig taps for (mode, slot) once the window has closed."""
    nm = nc.dev.mark()
    for k in range(presses):
        _press(ctl, i, hold_s)
        if k < presses - 1:
            time.sleep(gap_s)
    time.sleep(tap_ms / 1000 + 1.0)
    return [t for _, t in _taps(nc, nm, mode, slot)]


@test("sbus.matrix_debounce_n", "matrixDebounceFrames: at 1 a press of a few ms still commits; at 4 such a press "
      "never does, while single, double and triple taps of real presses still give taps 1, 2 and 3",
      needs=["sbus", "navicore"], links=[])
def matrix_debounce_n(bench):
    """processSbus commits a press only once the decoded slot has held for matrixDebounceFrames consecutive frames, and
    re-arms only after as many neutral ones (NaviCore.ino:2816-2876; clamped 1-4, rc_config.h:1560-1563). The controller
    sends a frame every 9 ms, so a press held 4-12 ms on the host is seen in at most two frames: at 1 it commits whenever
    a frame caught it, at 4 never. A press whose host-side hold ran past 12 ms is not counted. The button's slot has no
    mapping (rc_trig only). The debounce is set inside nc_guard (1 is also the bench's own value)."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    tap_ms = ncfg.get("tapWindowMs", 500)
    problems = []
    with nc_guard(bench) as g:
        try:
            if g.before.get("matrixDebounceFrames") != 1:
                g.nc.set_config({"matrixDebounceFrames": 1})
            c1, n1, h1 = _blips(ctl, g.nc, i, mode, slot, 10, tap_ms)
            g.nc.set_config({"matrixDebounceFrames": 4})
            time.sleep(0.3)
            c4, n4, h4 = _blips(ctl, g.nc, i, mode, slot, 10, tap_ms)
            taps = {p: _gesture(ctl, g.nc, i, mode, slot, p, tap_ms) for p in (1, 2, 3)}
        finally:
            ctl.button(i, False)
    bench.note(f"short presses: debounce 1 committed {c1} of {n1} (held {h1} ms); debounce 4 committed {c4} of {n4} "
               f"(held {h4} ms); taps at debounce 4: {taps}")
    if n1 < 5 or n4 < 5:
        raise Skip(f"too few presses stayed under 12 ms on the host ({n1}, {n4}): the PC is too busy for this test")
    if c1 == 0:
        problems.append(f"at debounce 1 none of {n1} short presses committed: the presses never reached NaviCore, so "
                        f"debounce 4's silence would prove nothing")
    if c4:
        problems.append(f"at debounce 4, {c4} of {n4} presses of at most 12 ms committed (two frames at most each)")
    for p, got in taps.items():
        if got != [p]:
            problems.append(f"at debounce 4, {p} press(es) gave taps {got}, expected [{p}]")
    assert not problems, "; ".join(problems)


@test("sbus.tap_saturation_4", "Four and five quick presses saturate at one tap-3 rc_trig (tap 4 is the long press, "
      "never a 4th tap), timed tapWindowMs after the last press", needs=["sbus", "navicore"], links=[])
def tap_saturation_4(bench):
    """RCRadio_Matrix_Buttons caps tapCount at 3 (NaviCore.ino:2330-2336, 'Deliberately 3, NOT RC_NUM_TAP_TIERS') and
    re-arms the deferred fire at every press; only the first press of a gesture opens a hold (:2345-2352), so the fire
    is timed from the last PRESS, not its release (checkDeferredTap :2404-2426). Unmapped slot: rc_trig only."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    tap_ms = ncfg.get("tapWindowMs", 500)
    problems = []
    try:
        for presses in (4, 5):
            nm = nc.dev.mark()
            last = None
            for k in range(presses):
                last, _ = _press(ctl, i, 0.1)
                if k < presses - 1:
                    time.sleep(0.12)
            time.sleep(tap_ms / 1000 + 1.2)
            trigs = _taps(nc, nm, mode, slot)
            if [t for _, t in trigs] != [3]:
                problems.append(f"{presses} presses gave taps {[t for _, t in trigs]}, expected [3]")
            elif not tap_ms - 50 <= (trigs[0][0] - last) * 1000 <= tap_ms + 300:
                problems.append(f"{presses} presses: rc_trig {(trigs[0][0] - last) * 1000:.0f} ms after the last press")
    finally:
        ctl.button(i, False)
    assert not problems, "; ".join(problems)


@test("sbus.other_button_commits", "Pressing a second button inside the first's tap window commits the first at once "
      "(its rc_trig comes with the second press, not tapWindowMs later), and the second then resolves on its own",
      needs=["sbus", "navicore"], links=[])
def other_button_commits(bench):
    """RCRadio_Matrix_Buttons dispatches a pending gesture first when a different button is pressed (NaviCore.ino:
    2316-2325), then starts a new single tap. Two controller buttons on slots unmapped in the current mode (RB1 and RB2
    here): rc_trig only. A's rc_trig must follow B's press within the debounce and loop latency, well before A's own
    window would have closed; B's comes tapWindowMs after B's release (a first press re-times from its release,
    rcMatrixRelease :2379-2402)."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    (ia, _, sa), (ib, _, sb) = _unmapped_buttons(nc, cfg, ncfg, [mode], 2)
    tap_ms = ncfg.get("tapWindowMs", 500)
    problems = []
    try:
        nm = nc.dev.mark()
        _, a_rel = _press(ctl, ia, 0.1)
        time.sleep(0.15)
        b_press, b_rel = _press(ctl, ib, 0.1)
        time.sleep(tap_ms / 1000 + 1.2)
        a, b = _taps(nc, nm, mode, sa), _taps(nc, nm, mode, sb)
    finally:
        ctl.button(ia, False)
        ctl.button(ib, False)
    if [t for _, t in a] != [1] or [t for _, t in b] != [1]:
        problems.append(f"slot {sa} taps {[t for _, t in a]}, slot {sb} taps {[t for _, t in b]}; expected one tap 1 "
                        f"each")
    else:
        da, db = (a[0][0] - b_press) * 1000, (b[0][0] - b_rel) * 1000
        bench.note(f"slot {sa} fired {da:.0f} ms after slot {sb}'s press; slot {sb} {db:.0f} ms after its release")
        if not -30 <= da <= 250 or a[0][0] >= a_rel + (tap_ms - 100) / 1000:
            problems.append(f"slot {sa} fired {da:.0f} ms after the other press: it was not committed by it")
        if not tap_ms - 50 <= db <= tap_ms + 300:
            problems.append(f"slot {sb} fired {db:.0f} ms after its release (tapWindowMs {tap_ms})")
        if a[0][0] > b[0][0]:
            problems.append("the second button's tap came before the first's")
    assert not problems, "; ".join(problems)


@test("sbus.mode_latched_at_press", "A tap dispatches in the mode it was pressed in: a mesh SET_MODE while the button is "
      "held does not change the rc_trig's mode; the next press uses the new one (mode changes move the mode-aware "
      "knobs' servos)", needs=["sbus", "navicore", "wcb1"], links=[])
def mode_latched_at_press(bench):
    """RCRadio_Matrix_Buttons stores deferredBtn = FunctionSwState * 100 + btn at the press (NaviCore.ino:2338-2343), and
    a first press is parked while held (checkDeferredTap :2404-2426), so the mode can change under it. A mesh SET_MODE
    (rc_telemetry.h:2236-2243) makes the change while the button is down; the slot has no mapping in either mode
    (rc_trig only; no tier 4, so the hold is not promoted). The mode is set back at the end."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    w1 = usb_wcb(bench)
    nid = nc.wcb_status()["self"]
    m0 = nc.mode()
    m1 = next(m for m in (2, 1, 3) if m != m0)
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [m0, m1], 1)
    tap_ms = ncfg.get("tapWindowMs", 500)
    problems = []
    try:
        nm = nc.dev.mark()
        ctl.button(i, True)
        time.sleep(0.15)
        w1.send(f';W{nid},{{"type":"SET_MODE","mode":{m1}}}')
        deadline = time.monotonic() + 2.5
        while nc.mode() != m1 and time.monotonic() < deadline:
            time.sleep(0.2)
        changed = nc.mode() == m1
        ctl.button(i, False)
        time.sleep(tap_ms / 1000 + 1.0)
        first = [k for _, k in _rc_trigs(nc, nm, {(m0, slot), (m1, slot)})]
        nm = nc.dev.mark()
        _press(ctl, i, 0.12)
        time.sleep(tap_ms / 1000 + 1.0)
        second = [k for _, k in _rc_trigs(nc, nm, {(m0, slot), (m1, slot)})]
    finally:
        ctl.button(i, False)
        w1.send(f';W{nid},{{"type":"SET_MODE","mode":{m0}}}')
        time.sleep(1.5)
    if not changed:
        problems.append(f"the mesh SET_MODE to {m1} did not take while the button was held")
    if first != [(m0, slot, 1)]:
        problems.append(f"the press made in mode {m0} dispatched as {first}")
    if second != [(m1, slot, 1)]:
        problems.append(f"the press made in mode {m1} dispatched as {second}")
    if nc.mode() != m0:
        problems.append(f"the mode was not set back to {m0}")
    assert not problems, "; ".join(problems)


@test("sbus.long_press_configured", "With tier 4 mapped, holding past holdMs fires tier 4 alone, at holdMs, while still "
      "held (rc_trig tap 4); a short tap and a hold just under holdMs each fire tier 1 on release",
      needs=["sbus", "navicore", "wcb1"], links=["W1S2"])
def long_press_configured(bench):
    """processSbus promotes a held first press to tier 4 once holdMs has passed and the slot's t4 has actions
    (NaviCore.ino:2870-2874, rcHoldTierConfigured :2359-2366, rcMatrixHoldFire :2368-2377), and a long press always
    dispatches alone (rcDispatch :2286-2298). A sub-threshold hold is an ordinary tap re-timed from its release
    (rcMatrixRelease :2379-2402). A guarded mapping on an unmapped button's slot: t1 and t4 one W1 S2 marker each."""
    l12 = link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        tap_ms, hold_ms = g.before.get("tapWindowMs", 500), g.before.get("holdMs", 750)
        t1, t4 = marker("H1"), marker("H4")
        nc.set_config({"mappings": {str(mode * 100 + slot): {"exclusive": False, "t1": [_act(t1)], "t4": [_act(t4)]}}})
        time.sleep(0.3)
        try:
            for label, hold_s, want in (("long press", (hold_ms + 500) / 1000, 4), ("short tap", 0.12, 1),
                                        ("hold under holdMs", max(0.12, (hold_ms - 250) / 1000), 1)):
                pm, nm = l12.mark(), nc.dev.mark()
                press, rel = _press(ctl, i, hold_s)
                time.sleep(tap_ms / 1000 + 1.0)
                trigs = _taps(nc, nm, mode, slot)
                got = l12.received(pm)
                if [t for _, t in trigs] != [want]:
                    problems.append(f"{label}: taps {[t for _, t in trigs]}, expected [{want}]")
                    continue
                at = (trigs[0][0] - press) * 1000
                if want == 4 and not (hold_ms - 30 <= at <= hold_ms + 250 and trigs[0][0] < rel):
                    problems.append(f"{label}: tier 4 fired {at:.0f} ms after the press (holdMs {hold_ms}), "
                                    f"{'before' if trigs[0][0] < rel else 'after'} the release")
                if want == 1 and not tap_ms - 50 <= (trigs[0][0] - rel) * 1000 <= tap_ms + 300:
                    problems.append(f"{label}: tier 1 fired {(trigs[0][0] - rel) * 1000:.0f} ms after the release")
                if (_count(got, t1), _count(got, t4)) != ((1, 0) if want == 1 else (0, 1)):
                    problems.append(f"{label}: markers t1 x{_count(got, t1)}, t4 x{_count(got, t4)} on W1 S2")
        finally:
            ctl.button(i, False)
            time.sleep(tap_ms / 1000 + 0.3)
    assert not problems, "; ".join(problems)


@test("sbus.held_second_tap_midhold", "A second tap that is held fires tap 2 tapWindowMs after it was pressed, while it "
      "is still held, and nothing on its release (today's behaviour, pinned; the code comment says it waits for the "
      "release)", needs=["sbus", "navicore"], links=[])
def held_second_tap_midhold(bench):
    """RCRadio_Matrix_Buttons opens a hold only on the first press of a gesture (holdActive = tapCount == 1,
    NaviCore.ino:2345-2352), so checkDeferredTap does not park a second press (:2404-2426) and rcMatrixRelease ignores
    its release (:2379-2381): tap 2 fires at the second press + tapWindowMs with the button still down. The comment
    beside that line says 'a held 2nd tap simply dispatches its tier when the button comes up', and ARCHITECTURE.md:308-309
    likewise (NAVICORE.md D-NC36's doc list): pinned here; the comment is the bug. Unmapped slot: rc_trig only."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    (i, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    tap_ms = ncfg.get("tapWindowMs", 500)
    try:
        nm = nc.dev.mark()
        _press(ctl, i, 0.1)
        time.sleep(0.15)
        press2, rel2 = _press(ctl, i, (tap_ms + 800) / 1000)
        time.sleep(tap_ms / 1000 + 1.0)
        trigs = _taps(nc, nm, mode, slot)
    finally:
        ctl.button(i, False)
    assert [t for _, t in trigs] == [2], f"taps {[t for _, t in trigs]}, expected [2]"
    at = (trigs[0][0] - press2) * 1000
    bench.note(f"tap 2 fired {at:.0f} ms after the second press; the button came up {(rel2 - press2) * 1000:.0f} ms "
               f"after it")
    assert trigs[0][0] < rel2, "tap 2 fired after the release: the held second tap was parked (the comment's behaviour)"
    assert tap_ms - 50 <= at <= tap_ms + 300, f"tap 2 fired {at:.0f} ms after the second press (tapWindowMs {tap_ms})"


# ------------------------------------------------------------------ switches
def _sw_tiers(tags, easing=None):
    """A switch's p0/p1/p2 tiers: `easing` actions for a position when given, then one W1 S2 marker."""
    return {f"p{p}": list((easing or {}).get(p, [])) + [_act(tags[p])] for p in range(3)}


@test("sbus.switch_tiers_settle_seed", "A switch tier fires once its position has held for switchSettleMs (never "
      "sooner, from the stick move); a sweep through the middle position faster than that never fires it; a save, or "
      "moving the switch to another channel, re-seeds its position without firing", needs=["sbus", "navicore", "wcb1"],
      links=["W1S2"])
def switch_tiers_settle_seed(bench):
    """processSwitches (NaviCore.ino:2428-2480): a new position is a candidate until it has held switchSettleMs, and only
    then runs its tier; g_switchSeedPending, set by every config apply (rc_config.h:1920) and at boot, adopts each
    switch's position without firing. A free switch (no tiers, not the mode function, no knob override) is rebound to
    the rx stick with a W1 S2 marker per position, inside nc_guard; switchSettleMs is raised to 400 ms there, so the
    lower bound stands well clear of the latencies (they only add to it) and a 150 ms pass through the middle is well
    inside it. readSwitchPos: < 582 is 0, > 1401 is 2 (:566-571); the stick rests at 992, position 1."""
    l12 = link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    ch2 = _stick(nc, cfg, ncfg, "ry")
    sticks = Sticks(ctl, cfg)
    settle = 400
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        free = _free_switches(g.before)
        if not free:
            raise Skip("no free switch to rebind")
        label = free[0][1]
        tags = {p: marker(f"W{p}") for p in range(3)}

        def to_position(value, pos, what):
            pm = l12.mark()
            t = sticks.set("rx", value)
            got = _wait_all(l12, pm, [tags[pos]], (settle + 1500) / 1000)
            at = _host_time(l12, pm, tags[pos].encode() + b"\r")
            if _count(got, tags[pos]) != 1:
                problems.append(f"{what}: position {pos}'s marker x{_count(got, tags[pos])}")
            elif at is not None:
                ms = (at - t) * 1000
                notes.append(f"{what} {ms:.0f} ms")
                if ms < settle - 10:
                    problems.append(f"{what}: position {pos} fired {ms:.0f} ms after the move, sooner than "
                                    f"switchSettleMs {settle}")
                if ms > settle + 900:
                    problems.append(f"{what}: position {pos} fired only {ms:.0f} ms after the move")
            return got
        try:
            pm = l12.mark()
            nc.set_config({"switchSettleMs": settle, "switches": {label: {"channel": ch, "positions": 3,
                                                                          **_sw_tiers(tags)}}})
            time.sleep(1.2)
            if l12.received(pm):
                problems.append(f"the save that bound {label} to CH{ch} fired a tier: {l12.received(pm)!r}")
            to_position(450, 0, "to position 0")
            problems += _check_value(nc, ch, 450, "position 0")
            for attempt in (1, 2):                     # 0 -> through 1 for ~150 ms -> 2
                pm = l12.mark()
                t_mid = sticks.set("rx", SBUS_CENTER)
                time.sleep(0.15)
                mid = (sticks.set("rx", 1600) - t_mid) * 1000
                got = _wait_all(l12, pm, [tags[2]], (settle + 1500) / 1000)
                if mid <= settle - 150:
                    break
                if attempt == 2:
                    raise Skip(f"the PC could not keep a pass through position 1 under {settle - 150} ms ({mid:.0f})")
                to_position(450, 0, "back to position 0")
            notes.append(f"a pass through position 1 of {mid:.0f} ms")
            if _count(got, tags[1]):
                problems.append(f"a {mid:.0f} ms pass through position 1 fired its tier")
            if _count(got, tags[2]) != 1:
                problems.append(f"after the sweep position 2's marker x{_count(got, tags[2])}")
            tags2 = {**tags, 2: marker("W2b")}
            pm = l12.mark()
            nc.set_config({"switches": {label: {"channel": ch, "positions": 3, **_sw_tiers(tags2)}}})
            time.sleep(1.5)
            if l12.received(pm):
                problems.append(f"a save while parked at position 2 fired: {l12.received(pm)!r}")
            pm = l12.mark()
            nc.set_config({"switches": {label: {"channel": ch2, "positions": 3, **_sw_tiers(tags2)}}})
            time.sleep(1.5)
            if l12.received(pm):
                problems.append(f"moving {label} to CH{ch2} (the other stick, at position 1) fired: "
                                f"{l12.received(pm)!r}")
        finally:
            sticks.center()
            time.sleep(settle / 1000 + 0.6)
    bench.note(f"{label} on the stick, switchSettleMs {settle}: " + "; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.switch_easing_seed", "A switch's setEasing tier is adopted at the seed without running the tier: after a "
      "save the knob it governs gets that profile's speed/accel frames on W1 S1 and the tier's marker does not fire; "
      "moving to a position runs its tier (marker, and the easing it names)", needs=["sbus", "navicore", "wcb1"],
      links=["W1S1", "W1S2"])
def switch_easing_seed(bench):
    """seedSwitchEasingFromTier (NaviCore.ino:1263-1305) sets the slot's switch easing from the resting position's
    setEasing, and the seed pass then drives it with reapplyMaestroEasing and two repeats 500 ms apart (processSwitches
    :2466-2478, easingRepeatTick :1235-1250); reapply writes speed/accel only where the cache differs (:1147-1170). A
    passthrough knob on channel 0 (never dispatched, processKnobs :2607-2608, yet counted by reapply) with one output
    on the remote slot's channel X and no profile of its own follows the switch (resolveKnobEasing :1010-1016). A free profile
    P gets (slot, X) = speed S, accel A, S random per run so a cache left by an earlier run cannot hide the write. A
    free switch on the rx stick: position 1 (the stick's rest) = setEasing,pP + marker, position 0 = setEasing,release +
    marker. The test ends on position 0 (release), as slot 4's easing started; the stick is centred after the restore."""
    l11, l12 = link(bench, 1, "S1"), link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    sticks = Sticks(ctl, cfg)
    slot, dev = _remote_slot(ncfg)
    (x,) = _free_channels(ncfg, slot, 1)
    s_spd, s_acc = random.randint(100, 400), random.randint(5, 50)
    problems = []
    try:
        with nc_guard(bench) as g:
            nc = g.nc
            knobs, switches, prof = _free_knobs(g.before), _free_switches(g.before), _free_profile(g.before)
            if not knobs or not switches or prof is None:
                raise Skip("no free knob, switch or smoothing profile to borrow")
            label, klab = switches[0][1], knobs[0]
            profiles = [dict(p) for p in g.before.get("smoothProfiles") or []]
            profiles[prof] = dict(profiles[prof], entries=[{"mid": slot, "ch": x, "spd": s_spd, "acc": s_acc}])
            tags = {p: marker(f"E{p}") for p in range(3)}
            ease = {0: [{"type": "maestro", "target": str(slot), "cmd": "setEasing,release"}],
                    1: [{"type": "maestro", "target": str(slot), "cmd": f"setEasing,p{prof}"}]}
            speed, accel = (CMD_SPEED, x, s_spd), (CMD_ACCEL, x, s_acc)
            zero = [(CMD_SPEED, x, 0), (CMD_ACCEL, x, 0)]
            m11, m12 = l11.mark(), l12.mark()
            nc.set_config({"smoothProfiles": profiles, "knobs": {klab: _knob(0, outputs=[_out(x, target=slot)])},
                           "switches": {label: {"channel": ch, "positions": 3, **_sw_tiers(tags, ease)}}})
            time.sleep(2.0)                           # the seed, then its two repeats 500 ms apart
            got = _frames(l11, m11, dev, {x})
            if speed not in got or accel not in got:
                problems.append(f"after the save W1 S1 got {got}: no speed {s_spd} / accel {s_acc} frames for the "
                                f"seeded profile")
            if len([f for f in got if f in (speed, accel)]) > 6:
                problems.append(f"after the save W1 S1 got {got}: the seed and its two repeats send 3 pairs at most")
            if l12.received(m12):
                problems.append(f"the seed ran a tier: {l12.received(m12)!r} on W1 S2")
            for value, pos, want in ((450, 0, "zero"), (SBUS_CENTER, 1, "profile")):
                m11, m12 = l11.mark(), l12.mark()
                sticks.set("rx", value)
                _wait_all(l12, m12, [tags[pos]], 2.0)
                time.sleep(1.5)
                got, marks = [f for f in _frames(l11, m11, dev, {x}) if f[0] != CMD_TARGET], l12.received(m12)
                if _count(marks, tags[pos]) != 1:
                    problems.append(f"moving to position {pos} did not run its tier once (marker "
                                    f"x{_count(marks, tags[pos])})")
                if want == "zero" and got != zero:
                    problems.append(f"setEasing,release sent {got}, expected {zero} (the knob's channel back to full "
                                    f"speed)")
                if want == "profile" and (speed not in got or accel not in got):
                    problems.append(f"setEasing,p{prof} run from its tier sent {got}")
            m12 = l12.mark()
            sticks.set("rx", 450)                     # end on setEasing,release
            _wait_all(l12, m12, [tags[0]], 2.0)
            time.sleep(0.5)
    finally:
        sticks.center()
    bench.note(f"slot {slot} ch {x}: profile {prof} = speed {s_spd}, accel {s_acc}")
    assert not problems, "; ".join(problems)


@test("sbus.mode_switch_decode", "The mode switch sets the mode from its value alone: SE's three positions give modes "
      "1-3 (#L12, the monitor); on a stick 581 is mode 1 and 587 mode 2, a move of 5 counts is not looked at (586 after "
      "581 stays 1, 1405 after 1400 stays 2), 1406 is mode 3; a 2-position switch gives 1 and 3 all the same (each mode "
      "change moves the mode-aware knobs' servos)", needs=["sbus", "navicore", "wcb1"], links=[])
def mode_switch_decode(bench):
    """processSbus: when the bound switch's value is more than 5 counts from the last one evaluated, the mode is < 582 ->
    1, < 1401 -> 2, else 3, whatever the switch's `positions` (NaviCore.ino:2799-2812); a change re-arms the mode-aware
    knobs and broadcasts rc_mode. Part 1 moves the real mode switch (the controller switch on the mode function's
    channel) through its positions and back; the rc_mode lines W1 relays meanwhile are noted, not asserted (a
    best-effort broadcast, as navicore.set_mode treats it). Part 2, inside nc_guard, points the mode function at a
    free switch rebound to the rx stick, pre-positioned so the rebinding itself changes nothing, and steps across the
    boundaries and the deadband; then the same switch as 2-position. The mode ends where it started (the guard re-sends
    it over the mesh if not); the stick is centred after the restore, when it drives nothing."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    sticks = Sticks(ctl, cfg)
    w1 = usb_wcb(bench)
    fb = (ncfg.get("funcBindings") or {}).get("mode", -1)
    if not 0 <= fb < len(SWITCHES):
        raise Skip("NaviCore has no mode switch bound")
    mode_ch = ncfg["switches"][SWITCHES[fb]]["channel"]
    k = next((i for i, s in enumerate(cfg.get("sw") or []) if s.get("c") == mode_ch), None)
    problems, notes = [], []

    def expect_mode(want, what, wait=0.35):
        time.sleep(wait)
        got = nc.mode()
        notes.append(f"{what}: {got}")
        if got != want:
            problems.append(f"{what}: mode {got}, expected {want}")

    def decode_mode(v):
        return 1 if v < 582 else 2 if v < 1401 else 3
    try:
        with nc_guard(bench) as g:
            nc = g.nc
            try:
                if k is None:
                    notes.append(f"no controller switch on the mode channel CH{mode_ch}: part 1 left out")
                else:
                    sw = cfg["sw"][k]
                    p0 = sw.get("pos", 0)
                    wm = w1.dev.mark()
                    bridged(w1, {"type": "PING"}, r'"type":"PONG"', timeout=3.0)   # W1 relays rc_mode for 20 s
                    for p in [q for q in (1, 2, 0) if q != p0] + [p0]:
                        ctl.switch(k, p)
                        expect_mode(decode_mode(sw["v"][p]), f"{sw.get('l')} position {p} ({sw['v'][p]})")
                    relayed = [int(r.group(1)) for x in w1.dev.since(wm)
                               for r in [re.match(r'^\{"sys":1,"type":"rc_mode","id":\d+,"mode":(\d)', x)] if r]
                    notes.append(f"W1 relayed rc_mode {relayed} for the three changes (a best-effort broadcast: noted)")
                    frames = nc.monitor(0.6)
                    if frames and (frames[-1].get("modeCh"), frames[-1].get("mode")) != (mode_ch, nc.mode()):
                        problems.append(f"the monitor shows modeCh {frames[-1].get('modeCh')} mode "
                                        f"{frames[-1].get('mode')}")
            finally:
                if k is not None:
                    ctl.switch(k, cfg["sw"][k].get("pos", 0))
            free = _free_switches(g.before)
            if not free:
                raise Skip("no free switch to bind the mode function to")
            idx, label = free[0]
            m_now = nc.mode()
            sticks.set("rx", MODE_VALUE[m_now])
            time.sleep(0.3)
            nc.set_config({"funcBindings": {"mode": idx}, "switches": {label: {"channel": ch, "positions": 3}}})
            expect_mode(m_now, "after the rebinding", 0.5)
            frames = nc.monitor(0.6)
            if frames and frames[-1].get("modeCh") != ch:
                problems.append(f"the monitor's modeCh is {frames[-1].get('modeCh')}, not CH{ch}")
            for value, want, what in ((400, 1, "400"), (575, 1, "575"), (581, 1, "581"), (586, 1, "586 (5 after 581)"),
                                      (587, 2, "587"), (1394, 2, "1394"), (1400, 2, "1400"),
                                      (1405, 2, "1405 (5 after 1400)"), (1406, 3, "1406"), (400, 1, "back to 400")):
                sticks.set("rx", value)
                expect_mode(want, what)
                if value in (581, 586, 587, 1405):
                    problems += _check_value(nc, ch, value, what)
            nc.set_config({"switches": {label: {"channel": ch, "positions": 2}}})
            for value, want in ((1600, 3), (400, 1)):
                sticks.set("rx", value)
                expect_mode(want, f"2-position {value}")
            sticks.set("rx", MODE_VALUE[m_now])
            expect_mode(m_now, "back to the starting mode")
    finally:
        sticks.center()
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ knobs
@test("sbus.knob_passthrough_remote", "A passthrough knob sends posMin + (v-172)(posMax-posMin)/1639 to its Maestro "
      "channel, exactly, once per move of 5 counts or more (4 sends nothing); reverse flips the stick, midClosed maps "
      "only its upper half, reversed endpoints ramp down (remote slot 4: nothing moves)",
      needs=["sbus", "navicore", "wcb1"], links=["W1S1"])
def knob_passthrough_remote(bench):
    """processKnobs (NaviCore.ino:2598-2700): the deadband is |raw - lastKnobRaw| < 5 (KNOB_CHANGE_DEADBAND, :2577), and
    lastKnobRaw moves only on a dispatch; reverse is 1983 - raw before anything else (:2642-2648); the maps are
    sbusToRange / sbusToRangeMidClosed (:593-627) in C long arithmetic, clamped to the endpoints in either order. A free
    knob on the rx stick, one output on the remote slot's channel X, inside nc_guard. A save that sets reverse sends the
    flipped position at once, since lastKnobRaw survives the save; one that changes only the endpoints sends nothing
    until the stick moves. The frames are read on W1 S1."""
    ctl, nc, cfg, ncfg, sticks, ch, l11, slot, dev, (x,), wire = _knob_rig(bench)
    w = Watch11(l11, nc, dev, [x], wire, witnesses=_witnesses(bench))
    tgt = CMD_TARGET
    with nc_guard(bench) as g:
        nc = w.nc = g.nc
        knobs = _free_knobs(g.before)
        if not knobs:
            raise Skip("no free knob to borrow")
        lab = knobs[0]
        try:
            with nc.debug(DBG_WIRE if wire else 0):
                nc.set_config({"knobs": {lab: _knob(ch, outputs=[_out(x, target=slot)])}})
                time.sleep(1.0)
                w.step(lambda: sticks.set("rx", 1500), [(tgt, x, knob_pos(1500, 4000, 8000))], "1500")
                w.step(lambda: sticks.set("rx", 1504), [], "1504 (4 counts)")
                w.problems += _check_value(nc, ch, 1504, "the 4-count step")
                w.step(lambda: sticks.set("rx", 1505), [(tgt, x, knob_pos(1505, 4000, 8000))], "1505 (5 counts)")
                w.step(lambda: sticks.set("rx", 600), [(tgt, x, knob_pos(600, 4000, 8000))], "600")
                w.step(lambda: nc.set_config({"knobs": {lab: _knob(ch, reverse=True, outputs=[_out(x, target=slot)])}}),
                       [(tgt, x, knob_pos(600, 4000, 8000, reverse=True))], "the save that sets reverse")
                w.step(lambda: sticks.set("rx", 1200), [(tgt, x, knob_pos(1200, 4000, 8000, reverse=True))],
                       "reverse, 1200")
                mc = [_out(x, target=slot, midClosed=True)]
                w.step(lambda: nc.set_config({"knobs": {lab: _knob(ch, outputs=mc)}}),
                       [(tgt, x, knob_pos(1200, 4000, 8000, mid_closed=True))], "the save that sets midClosed")
                for v in (900, 1600):
                    w.step(lambda v=v: sticks.set("rx", v), [(tgt, x, knob_pos(v, 4000, 8000, mid_closed=True))],
                           f"midClosed, {v}")
                down = [_out(x, 7000, 5000, target=slot)]
                w.step(lambda: nc.set_config({"knobs": {lab: _knob(ch, outputs=down)}}), [], "posMin 7000 / posMax 5000")
                for v in (1300, SBUS_CENTER):
                    w.step(lambda v=v: sticks.set("rx", v), [(tgt, x, knob_pos(v, 7000, 5000))], f"7000..5000, {v}")
        finally:
            sticks.center()
            time.sleep(0.5)
    if w.lost:
        bench.note("an unacknowledged broadcast lost at W1 only: " + "; ".join(w.lost))
    w.problems += w.lost_problem()
    assert not w.problems, "; ".join(w.problems)


@test("sbus.knob_mode_aware", "A mode-aware knob drives its mode's output set and snaps to the stick on a mode change "
      "(one frame, for the new mode's channel); a knob that follows its own switch is not re-armed by the change (two "
      "mesh SET_MODEs: the bench's mode-aware knobs move their servos too)", needs=["sbus", "navicore", "wcb1"],
      links=["W1S1"])
def knob_mode_aware(bench):
    """processKnobs uses outputs / outputs2 / outputs3 for mode 1 / 2 / 3 when modeAware (NaviCore.ino:2614-2633);
    resetModeAwareKnobs, called on every global mode change (SET_MODE, rc_telemetry.h:2236-2243), re-arms only the
    mode-aware knobs with no modeSwitchOverride (NaviCore.ino:2709-2720), and a re-armed knob dispatches at the current
    stick value on the next frame. Two free knobs on the rx stick: K with a different remote-slot channel per mode, K2
    with one channel in every set and modeSwitchOverride on a free switch rebound to the resting ry stick. A switch
    with no channel would not do: it reads -1, and the knob then follows the global mode after all (NaviCore.ino:
    2622-2623), so it would re-dispatch at every change. The bench's J4 also sends a frame for slot 4 at each change,
    on channel 0, which these channels (5 and up) leave out."""
    ctl, nc, cfg, ncfg, sticks, ch, l11, slot, dev, chans, wire = _knob_rig(bench, 4)
    ch2 = _stick(nc, cfg, ncfg, "ry")
    x1, x2, x3, x4 = chans
    w = Watch11(l11, nc, dev, chans, wire, witnesses=_witnesses(bench))
    w1 = usb_wcb(bench)
    nid = nc.wcb_status()["self"]
    tgt = CMD_TARGET
    with nc_guard(bench) as g:
        nc = w.nc = g.nc
        knobs, switches = _free_knobs(g.before), _free_switches(g.before)
        if len(knobs) < 2 or not switches:
            raise Skip("fewer than two free knobs, or no switch for the override")
        (k1, k2), (ovr, ovr_label) = knobs[:2], switches[0]
        m0 = nc.mode()
        m1 = next(m for m in (2, 1, 3) if m != m0)
        per_mode = {1: x1, 2: x2, 3: x3}
        one = [_out(x4, target=slot)]

        def setmode(m):
            w1.send(f';W{nid},{{"type":"SET_MODE","mode":{m}}}')
            deadline = time.monotonic() + 2.5
            while nc.mode() != m and time.monotonic() < deadline:
                time.sleep(0.2)
        try:
            nc.set_config({"switches": {ovr_label: {"channel": ch2, "positions": 3}}, "knobs": {
                k1: _knob(ch, modeAware=True, outputs=[_out(x1, target=slot)], outputs2=[_out(x2, target=slot)],
                          outputs3=[_out(x3, target=slot)]),
                k2: _knob(ch, modeAware=True, modeSwitchOverride=ovr, outputs=one, outputs2=one, outputs3=one)}})
            time.sleep(1.2)
            p15, p12, p10 = (knob_pos(v, 4000, 8000) for v in (1500, 1200, SBUS_CENTER))
            w.step(lambda: sticks.set("rx", 1500), [(tgt, per_mode[m0], p15), (tgt, x4, p15)], f"1500 in mode {m0}")
            w.step(lambda: setmode(m1), [(tgt, per_mode[m1], p15)], f"SET_MODE {m1}")
            w.step(lambda: sticks.set("rx", 1200), [(tgt, per_mode[m1], p12), (tgt, x4, p12)], f"1200 in mode {m1}")
            w.step(lambda: setmode(m0), [(tgt, per_mode[m0], p12)], f"SET_MODE {m0}")
            w.step(lambda: sticks.set("rx", SBUS_CENTER), [(tgt, per_mode[m0], p10), (tgt, x4, p10)], "centre")
        finally:
            sticks.center()
            if nc.mode() != m0:
                setmode(m0)
    if w.lost:
        bench.note("an unacknowledged broadcast lost at W1 only: " + "; ".join(w.lost))
    w.problems += w.lost_problem()
    assert not w.problems, "; ".join(w.problems)


@test("sbus.knob_auto_release", "A passthrough output with releaseIdleMs sends Set Target 0 that long after the last "
      "move (timed on W1 S1), a move re-arms it, and a config save drops a pending release until the next move",
      needs=["sbus", "navicore", "wcb1"], links=["W1S1"])
def knob_auto_release(bench):
    """maestroIdleReleaseTick (NaviCore.ino:2731-2752) scans every 50 ms and sends setTarget 0 once a channel has been
    idle for its output's releaseIdleMs, which processKnobs copies at each dispatch (:2692-2693); maestroSetTarget
    re-arms the idle timer on every real move (:1033-1058). Every config apply runs resetMaestroReleaseState
    (applyConfigSideEffects :3333-3356), which forgets the policy until the knob dispatches again, so a servo resting
    across a save is never released. A free knob on the rx stick, release 1000 ms, output on the remote slot's channel
    X; the save is a no-op one, well inside the second."""
    ctl, nc, cfg, ncfg, sticks, ch, l11, slot, dev, (x,), wire = _knob_rig(bench)
    rel, tgt = 1000, CMD_TARGET
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        knobs = _free_knobs(g.before)
        if not knobs:
            raise Skip("no free knob to borrow")

        def move_and_release(v, what, save_first=False):
            m11 = l11.mark()
            sticks.set("rx", v)
            _wait_frames(l11, m11, dev, {x}, 1, 2.0)
            if save_first:
                time.sleep(0.15)
                nc.set_config({"chRateHz": g.before.get("chRateHz", 5)})
                time.sleep(2.5 * rel / 1000)
                got = _frames(l11, m11, dev, {x})
                mv = (tgt, x, knob_pos(v, 4000, 8000))
                # The settle resend may or may not go out: the save lands about 250 ms after the move, where the resend
                # is due, and a config apply drops a resend not yet sent. What counts is that no release follows.
                if got not in ([mv], [mv, mv]):
                    problems.append(f"{what}: W1 S1 got {got}; the save must drop the pending release")
                return
            mv = (tgt, x, knob_pos(v, 4000, 8000))
            got = _wait_frames(l11, m11, dev, {x}, 3, (rel + 1500) / 1000)
            if got != [mv, mv, (tgt, x, 0)]:      # the move, NaviCore's settle resend 250 ms on, then the release
                problems.append(f"{what}: W1 S1 got {got}, expected the move, its settle resend and then Set Target 0")
                return
            t_move = _probe_ms(l11, m11, _pololu(dev, tgt, x, knob_pos(v, 4000, 8000)))
            t_rel = _probe_ms(l11, m11, _pololu(dev, tgt, x, 0))
            if t_move is not None and t_rel is not None:
                notes.append(f"{what} {t_rel - t_move} ms")
                if not rel - 30 <= t_rel - t_move <= rel + 250:
                    problems.append(f"{what}: released {t_rel - t_move} ms after the move (releaseIdleMs {rel})")
        try:
            nc.set_config({"knobs": {knobs[0]: _knob(ch, outputs=[_out(x, target=slot, releaseIdleMs=rel)])}})
            time.sleep(rel / 1000 + 1.0)               # a knob primed earlier dispatches at the save, then releases
            move_and_release(1500, "1500")
            move_and_release(1300, "1300 (re-armed)")
            move_and_release(1100, "1100, then a save", save_first=True)
            move_and_release(1300, "1300 after the save")
        finally:
            sticks.center()
            time.sleep(rel / 1000 + 0.5)
    bench.note("released after the move (probe clock): " + "; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.knob_settle_resend", "A remote passthrough knob's last setTarget goes out once more when the knob has "
      "been still 250 ms (its stream is an unacknowledged broadcast): one move puts its frame and the same frame again "
      "200-450 ms later on W1 S1, three quick moves put three frames and one resend of the last, and an auto-release "
      "(100 ms) before the resend is due drops it (remote slot 4: nothing moves)", needs=["sbus", "navicore", "wcb1"],
      links=["W1S1"])
def knob_settle_resend(bench):
    """NaviCore KNOB_SETTLE_RESEND_MS (navicore-fix9, NaviCore 131c18d): processKnobs arms a resend for each output it
    sends to a remote slot, and knobResendTick writes the frame once more after 250 ms still, unless the channel was
    written or released since. Added because W1 missed single knob frames W3 forwarded, which left the servo at the
    previous position until the next move (WCB runs 20261009-182550, 20261010-000816). The resend is the frame alone,
    so auto-release still times from the move (sbus.knob_auto_release)."""
    ctl, nc, cfg, ncfg, sticks, ch, l11, slot, dev, (x,), wire = _knob_rig(bench)
    problems, notes = [], []

    def frame(v):
        return (CMD_TARGET, x, knob_pos(v, 4000, 8000))

    def check(what, moves, want, gap_of=None, gap_s=0.08):
        m = l11.mark()
        for v in moves:
            sticks.set("rx", v)
            time.sleep(gap_s)
        time.sleep(1.2)
        got = _frames(l11, m, dev, {x})
        gap = None
        if gap_of is not None:
            data = _pololu(dev, CMD_TARGET, x, knob_pos(gap_of, 4000, 8000))
            t0, t1 = _probe_ms(l11, m, data, 0), _probe_ms(l11, m, data, 1)
            gap = None if t0 is None or t1 is None else t1 - t0
        notes.append(f"{what}: {[f[2] for f in got]}" + (f", resend +{gap} ms" if gap_of is not None else ""))
        if got != want:
            problems.append(f"{what}: W1 S1 got {got}, expected {want}")
        elif gap_of is not None and (gap is None or not 200 <= gap <= 450):
            problems.append(f"{what}: the resend came {gap} ms after the frame, not about 250 ms")
    with nc_guard(bench) as g:
        nc = g.nc
        knobs = _free_knobs(g.before)
        if not knobs:
            raise Skip("no free knob to borrow")
        try:
            nc.set_config({"knobs": {knobs[0]: _knob(ch, outputs=[_out(x, target=slot)])}})
            time.sleep(1.2)
            check("one move", [1500], [frame(1500), frame(1500)], gap_of=1500)
            check("three moves 80 ms apart", [1300, 1100, 900], [frame(1300), frame(1100), frame(900), frame(900)],
                  gap_of=900)
            nc.set_config({"knobs": {knobs[0]: _knob(ch, outputs=[_out(x, target=slot, releaseIdleMs=100)])}})
            time.sleep(1.2)
            check("auto-release at 100 ms", [1400], [frame(1400), (CMD_TARGET, x, 0)])
        finally:
            sticks.center()
            time.sleep(0.8)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.knob_easing_resolve", "A knob's own easing profile goes out when the easing changes, not with every move: "
      "after a save its channel gets the profile's speed/accel (the write and two repeats), stick moves send targets "
      "only, and changing the profile's speed sends the new speed", needs=["sbus", "navicore", "wcb1"], links=["W1S1"])
def knob_easing_resolve(bench):
    """resolveKnobEasing: a knob's own smoothProfile wins (NaviCore.ino:1010-1016); processKnobs sends speed/accel only
    when the cache differs (:2672-2683); applyConfigSideEffects re-applies at every save (reapplyMaestroEasing,
    :1147-1170, cache-gated), and the seed that follows every apply schedules two unconditional repeats of each positive
    limit 500 ms apart (:2466-2478, reassertMaestroEasing :1212-1233). A free knob on the rx stick with its own free
    profile P: (slot, X) = speed S, accel A (random per run). The Kyber broadcast is best effort, so a count on W1 S1 is
    at most what NaviCore sent."""
    ctl, nc, cfg, ncfg, sticks, ch, l11, slot, dev, (x,), wire = _knob_rig(bench)
    s1, s2, acc = random.randint(100, 300), random.randint(301, 500), random.randint(5, 50)
    problems = []

    def easing(since):
        return [f for f in _frames(l11, since, dev, {x}) if f[0] in (CMD_SPEED, CMD_ACCEL)]
    with nc_guard(bench) as g:
        nc = g.nc
        knobs, prof = _free_knobs(g.before), _free_profile(g.before)
        if not knobs or prof is None:
            raise Skip("no free knob or smoothing profile to borrow")
        lab = knobs[0]
        profiles = [dict(p) for p in g.before.get("smoothProfiles") or []]

        def with_speed(s):
            p = [dict(q) for q in profiles]
            p[prof] = dict(p[prof], entries=[{"mid": slot, "ch": x, "spd": s, "acc": acc}])
            return p
        try:
            m11 = l11.mark()
            nc.set_config({"smoothProfiles": with_speed(s1),
                           "knobs": {lab: _knob(ch, smoothProfile=prof, outputs=[_out(x, target=slot)])}})
            time.sleep(1.8)
            got = easing(m11)
            want = {(CMD_SPEED, x, s1), (CMD_ACCEL, x, acc)}
            if not want <= set(got) or set(got) - want or not 2 <= len(got) <= 6:
                problems.append(f"after the save W1 S1 got {got}; expected speed {s1} and accel {acc}, 1-3 times each")
            m11 = l11.mark()
            for v in (1500, 1300, 1100):
                sticks.set("rx", v)
                time.sleep(0.4)
            time.sleep(0.4)
            fr = _frames(l11, m11, dev, {x})
            if [f for f in fr if f[0] != CMD_TARGET]:
                problems.append(f"stick moves re-sent easing: {[f for f in fr if f[0] != CMD_TARGET]}")
            # Each move 0.4 s apart, so each is followed by NaviCore's settle resend (navicore-fix9) 250 ms on.
            targets = [f for f in fr if f[0] == CMD_TARGET]
            want_t = [(CMD_TARGET, x, knob_pos(v, 4000, 8000)) for v in (1500, 1300, 1100) for _ in (0, 1)]
            if targets != want_t:
                problems.append(f"3 stick moves gave targets {targets}, expected each one and its settle resend")
            m11 = l11.mark()
            nc.set_config({"smoothProfiles": with_speed(s2)})
            time.sleep(1.8)
            got = easing(m11)
            speeds, accels = [f for f in got if f[0] == CMD_SPEED], [f for f in got if f[0] == CMD_ACCEL]
            if (CMD_SPEED, x, s2) not in speeds or any(f != (CMD_SPEED, x, s2) for f in speeds) or len(speeds) > 3:
                problems.append(f"after the speed change W1 S1 got {got}; expected speed {s2}, the write and two "
                                f"repeats")
            if any(f[2] != acc for f in accels) or len(accels) > 2:
                problems.append(f"after the speed change the accel frames were {accels}: only the two repeats resend "
                                f"an unchanged accel")
        finally:
            sticks.center()
            time.sleep(0.5)
    bench.note(f"slot {slot} ch {x}, profile {prof}: speed {s1} then {s2}, accel {acc}")
    assert not problems, "; ".join(problems)


@test("sbus.knob_hcr_volume", "An HCR-volume knob sends ;H,VOL,<chan>,<v> to the HCR's WCB at most once per 80 ms, the "
      "last value of a sweep lands after it stops, a value over 99 is sent as 99, and a repeat is not sent",
      needs=["sbus", "navicore", "wcb1"], links=[])
def knob_hcr_volume(bench):
    """dispatchHcrVolume (NaviCore.ino:2529-2552): clamp to 99 (the knob; the HCR codec takes 100, WcbHcr.cpp case
    17), drop a value equal to the last one sent, and within 80 ms of the last send latch it instead
    (HCR_VOLUME_MIN_INTERVAL_MS); hcrVolFlushTick sends the latched value once the interval has passed (:2554-2566).
    With hcrDest on a WCB the send is ';H,VOL,<V|A|B>,<v>' (hcrFormatWcbCommand :1499-1553), traced under DBG_HCR as
    '[DISPATCH] HCR→WCB<n>  <cmd>  OK' (executeHcrAction :1600-1655); the trace's host timestamps time the sends, to
    within the USB flush (kickUsbCdcTx, 20 ms), hence the 50 ms floor. A free knob on the rx stick, function 2, output
    on audio channel B (the bench's own volume knobs drive V and A), posMin 0 / posMax 150. What the HCR's WCB does
    with it is NC-WP7's ncdev.hcr_remote_verbs."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    dest = ncfg.get("hcrDest") or {}
    if dest.get("transport") != "wcb":
        raise Skip(f"hcrDest is {dest.get('transport')!r}: this test reads the WCB transport's trace")
    sticks = Sticks(ctl, cfg)
    rx = re.compile(r"^\[DISPATCH\] HCR→WCB\d+  ;H,VOL,B,(\d+)  (OK|FAIL)")

    def sends(since):
        return [(ts, int(m.group(1))) for ts, x in list(nc.dev.lines[since:]) for m in [rx.match(x)] if m]

    def vol(v):
        return min(99, knob_pos(v, 0, 150))
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        knobs = _free_knobs(g.before)
        if not knobs:
            raise Skip("no free knob to borrow")
        try:
            with nc.debug(DBG_HCR):
                nc.set_config({"knobs": {knobs[0]: _knob(ch, KF_HCR_VOLUME, [_out(0, 0, 150, target=2)])}})
                sticks.set("rx", 400)
                time.sleep(1.0)
                nm = nc.dev.mark()
                for v in range(450, 1001, 50):
                    sticks.set("rx", v)
                    time.sleep(0.025)
                time.sleep(0.8)
                _flushed(nc, nm, 0.05)
                sweep = sends(nm)
                gaps = [round((b[0] - a[0]) * 1000) for a, b in zip(sweep, sweep[1:])]
                bench.note(f"sweep 450-1000 (12 steps, 25 ms apart): sent {[v for _, v in sweep]}, {gaps} ms apart")
                if not sweep or sweep[-1][1] != vol(1000):
                    problems.append(f"the sweep's last send was {sweep[-1][1] if sweep else None}, not {vol(1000)}: the "
                                    f"final value did not land")
                if any(gp < 50 for gp in gaps):
                    problems.append(f"sends {gaps} ms apart: the limit is one per 80 ms")
                if len(sweep) >= 12:
                    problems.append(f"{len(sweep)} sends for 12 steps in 300 ms: nothing was rate-limited")
                # Two positions whose volumes are both over 99 (the knob spans 0-150, so anything above ~1254):
                # 1600/1610 skipped the whole test on a controller whose rx stick tops out at 1606 (run 20260928-212848).
                nm = nc.dev.mark()
                sticks.set("rx", 1500)
                time.sleep(0.6)
                sticks.set("rx", 1510)
                time.sleep(0.6)
                _flushed(nc, nm, 0.05)
                top = [v for _, v in sends(nm)]
                if top != [99]:
                    problems.append(f"1500 then 1510 (volumes {knob_pos(1500, 0, 150)}, {knob_pos(1510, 0, 150)}) sent "
                                    f"{top}, expected 99 once")
        finally:
            sticks.center()
            time.sleep(0.5)
    assert not problems, "; ".join(problems)


@test("sbus.calibration_mutes_knobs", "Under CALIB a passthrough knob sends nothing, and once PING ends CALIB it sends "
      "the stick's current position; the idle auto-release is not gated by CALIB (today's behaviour, pinned)",
      needs=["sbus", "navicore", "wcb1"], links=["W1S1"])
def calibration_mutes_knobs(bench):
    """processKnobs returns at once while calibrationActive (NaviCore.ino:2598-2604), without moving lastKnobRaw, so the
    first frame after CALIB ends dispatches the current value. maestroIdleReleaseTick checks only isReplaying
    (:2731-2740), so a release that comes due under CALIB goes out. PING clears CALIB (:3855-3858). A free knob on the
    rx stick, release 700 ms, output on the remote slot's channel X."""
    ctl, nc, cfg, ncfg, sticks, ch, l11, slot, dev, (x,), wire = _knob_rig(bench)
    rel, tgt = 700, CMD_TARGET
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        knobs = _free_knobs(g.before)
        if not knobs:
            raise Skip("no free knob to borrow")
        try:
            nc.set_config({"knobs": {knobs[0]: _knob(ch, outputs=[_out(x, target=slot, releaseIdleMs=rel)])}})
            time.sleep(rel / 1000 + 1.3)
            m11 = l11.mark()
            sticks.set("rx", 1500)
            _wait_frames(l11, m11, dev, {x}, 1, 2.0)
            nc.calib(True)
            sticks.set("rx", 1200)
            time.sleep(rel / 1000 + 0.8)
            got = _frames(l11, m11, dev, {x})
            if got != [(tgt, x, knob_pos(1500, 4000, 8000)), (tgt, x, 0)]:
                problems.append(f"under CALIB W1 S1 got {got}; expected the move made before CALIB, then its release, "
                                f"and nothing for the move to 1200")
            m11 = l11.mark()
            nc.ping()
            got = _wait_frames(l11, m11, dev, {x}, 1, 2.0)
            if got[:1] != [(tgt, x, knob_pos(1200, 4000, 8000))]:
                problems.append(f"after PING W1 S1 got {got}; expected the stick's current position")
        finally:
            nc.calib(False)
            sticks.center()
            time.sleep(rel / 1000 + 0.5)
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ the reader under load and after stalls
def _samples_ok(samples, base):
    """Problems in #L09 samples (parse_sbus_dump dicts) against the baseline one: full rate, the same variant, no lost
    or failsafe flag, a frame counter that only rises."""
    out, last = [], int(base["frames"] or 0)
    for k, s in enumerate(samples):
        if s["fps"] < SBUS_FULL_FPS:
            out.append(f"sample {k}: fps {s['fps']}")
        if s["variant"] != base["variant"]:
            out.append(f"sample {k}: variant {s['variant']}")
        if s["lost"] != "no" or s["failsafe"] != "no":
            out.append(f"sample {k}: lost={s['lost']} failsafe={s['failsafe']}")
        if int(s["frames"] or 0) <= last:
            out.append(f"sample {k}: frame counter {s['frames']} did not rise from {last}")
        last = int(s["frames"] or 0)
    return out


def _monitor_frames(lines):
    out = []
    for x in lines:
        if x.startswith('{"type":"PWM_UPDATE"'):
            try:
                out.append(json.loads(x))
            except ValueError:
                pass
    return out


@test("sbus.lock_under_load", "The SBUS reader keeps its lock at full rate while NaviCore is loaded - every debug "
      "family on, the monitor streaming, bursts of ?MAE,GET on its local Maestro, GET_CONFIG: fps stays up, the "
      "variant never changes, no lost or failsafe flag, the frame counter only rises (reads only)",
      needs=["sbus", "navicore"], links=[])
def lock_under_load(bench):
    """The reader locks after 3 structurally valid frames and flushes on structure once locked (sbus_reader.h
    tryParseAndReset, the eager length flush); every hot-path log line is non-blocking (vlogf, NaviCore.ino:1569-1598;
    sendPWMUpdate's guard, :3168-3181); a local Maestro read blocks at most 25 ms (maestroLocalQuery :725-752). The
    load: SET_DEBUG_FLAGS with every dispatch family, START_MONITOR (a frame every 50 ms), then four rounds of five
    ?MAE,GET on the first local slot and a GET_CONFIG, with #L09 sampled each round and every monitor frame checked.
    Nothing is written and nothing moves, so it is not in hil/servos.py. Whether a stall past ~100 ms can make the
    reader decode garbage is sbus.stall_no_phantom's question; that test makes everything inert before it stalls."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    slots = nc.local_slots(ncfg)
    # #L09's fps is the frame count of the last whole second loop() timed (NaviCore.ino sbusFpsCounter), which can end
    # up to a second before the read: a sample taken soon after a stall counts the stall. The test before this one ends
    # with nc_guard's config save, a ~0.45 s LittleFS write that stalls loop(); sample 0 once read fps 48 for it while
    # the frame counter ran at full rate under the load itself (20261009-082225). Two seconds put it out of every window.
    time.sleep(FPS_WINDOW_CLEAR_S)
    base = nc.sbus_dump()
    samples, problems = [], []
    m = nc.dev.mark()
    try:
        nc.set_debug_flags(FLAGS_ALL)
        nc.ack({"type": "START_MONITOR"})
        for _ in range(4):
            for c in range(5):
                if slots:
                    nc.cli(f"?MAE,GET,{slots[0][0]},{c}")
            nc.config()
            samples.append(nc.sbus_dump())
            time.sleep(0.3)
    finally:
        nc.ack({"type": "STOP_MONITOR"})
        nc.set_debug_flags(0)
    frames = _monitor_frames(nc.dev.since(m))
    problems += _samples_ok(samples, base)
    bad = [f["sbus"] for f in frames if not f["sbus"].get("ok") or f["sbus"].get("failsafe") or f["sbus"].get("lost")]
    if bad:
        problems.append(f"{len(bad)} of {len(frames)} monitor frames show a bad stream, first {bad[0]}")
    counts = [f["sbus"].get("frames", 0) for f in frames]
    if counts != sorted(counts):
        problems.append("the monitor's frame counter went backwards")
    bench.note(f"{len(samples)} #L09 samples, fps {[s['fps'] for s in samples]}; {len(frames)} monitor frames")
    assert not problems, "; ".join(problems[:10])


@test("sbus.prefix_ambiguity_ch17", "With CH17 low (under 256) and CH18 a multiple of 32, SBUS-24 frame byte 24 is 0x00, "
      "so the frame's first 25 bytes look like a whole SBUS-16 frame: under load for 20 s NaviCore still reads SBUS-24, "
      "no phantom failsafe or lost flag, CH17 exact, and a matrix press still dispatches", needs=["sbus", "navicore"],
      links=[])
def prefix_ambiguity_ch17(bench):
    """sbus_reader.h: a 25-byte SBUS-16 frame is a byte prefix of a 36-byte SBUS-24 one whenever byte 24 is 0x00; read
    as 16, byte 23 (CH17's low bits) would be taken for the flags byte and could assert failsafe, freezing all
    dispatch. The reader guards it twice (a 25-byte flush waits for the next header; pendingLen16Check_ drops a false
    16-lock). Byte 24 = CH17 >> 8 | (CH18 & 0x1F) << 3, so on this bench (CH17 = 173 from switch SJ at rest, CH18 = 992
    from a slider) it already holds; otherwise the controller switch on CH17 is put at its lowest value, only when
    NaviCore's switch on CH17 sends nothing but text at that position, and put back. Load as in sbus.lock_under_load;
    one press of an unmapped matrix button halfway (rc_trig only)."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    (ib, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    frame = nc.sbus_raw()
    moved = None
    if len(frame) != 36 or frame[24] != 0:
        k = next((i for i, s in enumerate(cfg.get("sw") or []) if s.get("c") == 17), None)
        if len(frame) != 36 or k is None:
            raise Skip("frame byte 24 is not 0x00 and no controller switch on CH17 can make it so")
        sw = cfg["sw"][k]
        low = min(range(3), key=lambda p: sw["v"][p])
        swcfg = next((s for s in (ncfg.get("switches") or {}).values() if s.get("channel") == 17), None) or {}
        pos = 0 if sw["v"][low] < 582 else 1
        if any(a.get("type") not in ("wcb_broadcast", "wcb_unicast") for a in swcfg.get(f"p{pos}") or []):
            raise Skip("NaviCore's switch on CH17 would run more than text at that position")
        moved = (k, sw.get("pos", 0))
        ctl.switch(k, low)
        time.sleep(0.5)
        frame = nc.sbus_raw()
        if frame[24] != 0:
            ctl.switch(*moved)
            raise Skip(f"frame byte 24 is {frame[24]:02X} even with CH17 low")
    ch17 = decode(frame)["channels"][16]
    base = nc.sbus_dump()
    samples, problems, pressed = [], [], None
    m = nc.dev.mark()
    try:
        nc.set_debug_flags(FLAGS_ALL)
        nc.ack({"type": "START_MONITOR"})
        slots = nc.local_slots(ncfg)
        t_end = time.monotonic() + 20
        while time.monotonic() < t_end:
            for c in range(3):
                if slots:
                    nc.cli(f"?MAE,GET,{slots[0][0]},{c}")
            s = nc.sbus_dump()
            samples.append(s)
            if len(s["channels"]) > 16 and s["channels"][16] != ch17:
                problems.append(f"CH17 read {s['channels'][16]}, not {ch17}")
            if pressed is None and time.monotonic() > t_end - 10:
                pressed = nc.dev.mark()
                _press(ctl, ib, 0.12)
            time.sleep(0.5)
    finally:
        ctl.button(ib, False)
        nc.ack({"type": "STOP_MONITOR"})
        nc.set_debug_flags(0)
        if moved:
            ctl.switch(*moved)
            time.sleep(0.5)
    problems += _samples_ok(samples, base)
    bad = [f["sbus"] for f in _monitor_frames(nc.dev.since(m)) if f["sbus"].get("failsafe") or f["sbus"].get("lost")]
    if bad:
        problems.append(f"{len(bad)} monitor frames flag failsafe or a lost frame, first {bad[0]}")
    if pressed is None or not _taps(nc, pressed, mode, slot):
        problems.append("the matrix press made under load did not dispatch")
    bench.note(f"frame byte 23 {frame[23]:02X}, byte 24 {frame[24]:02X}; {len(samples)} samples over 20 s"
               + (f"; CH17 moved with controller switch {moved[0]}" if moved else ""))
    assert not problems, "; ".join(sorted(set(problems))[:10])


@test("sbus.stall_no_phantom", "After loop() stalls long enough to overflow the SBUS UART (20 stalls, the #L90 ones "
      "110-485 ms, every input still) NaviCore decodes no phantom frame: no detector knob sends a frame, no rc_trig, "
      "no dispatch, no mode change, no monitor excursion, and the stream recovers to full rate",
      needs=["sbus", "navicore", "wcb1"], links=["W1S1"])
def stall_no_phantom(bench):
    """The plan's nc.sbus.overflow_misalign hypothesis. Serial1 keeps the core's 256-byte RX ring (HardwareSerial.cpp:123,
    core 3.3.4; NaviCore sets only its TX buffer, NaviCore.ino:4658) beside the 128-byte FIFO, and no UART event task
    runs to flush it (none is created without a receive callback); SBUS-24 brings 4 bytes/ms, so a loop() stall past
    ~100 ms loses bytes and leaves one seam between old and new ones in the buffers. The reader,
    already locked, flushes a 36-byte buffer on header, length and footer alone (sbus_reader.h eager flush), and a
    buffer straddling the loss can pass them and decode once, with garbage channels. With the hook image the stalls are
    INF9's '#L90,<ms>' (16) plus four no-op USB saves, the real-world stall; without it, 20 saves. Everything that could
    act on a phantom is made inert first, inside nc_guard (s41 _engine_inert: no bands, no mode function, every knob
    off), and free knobs become detectors on the matrix channel, the mode switch's and the Maestro knobs', each passing
    through to its own channel of the remote slot: a decoded value 5 counts off its input sends a frame to W1 S1, where
    nothing moves. Also checked: rc_trig, [DISPATCH] lines other than the easing seed every save prints, #L12, the
    monitor's channels against #L09's, and the final fps."""
    l11 = link(bench, 1, "S1")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    slot, dev = _remote_slot(ncfg)
    hooks = _hooks(nc)
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        dets = _free_knobs(g.before)
        knobs = g.before.get("knobs") or {}
        watch = [ncfg.get("matrixChannel")]
        fb = (g.before.get("funcBindings") or {}).get("mode", -1)
        if 0 <= fb < len(SWITCHES):
            watch.append(g.before["switches"][SWITCHES[fb]]["channel"])
        watch += [k.get("channel") for k in knobs.values() if k.get("function") == KF_PASSTHROUGH]
        watch = [c for i, c in enumerate(watch) if c and c not in watch[:i]][:len(dets)]
        if not watch:
            raise Skip("no free knob to use as a detector")
        chans = _free_channels(g.before, slot, len(watch))
        off = _engine_inert(nc, g.before)
        nc.set_config({"knobs": {dets[i]: _knob(c, outputs=[_out(chans[i], target=slot)]) for i, c in enumerate(watch)}})
        time.sleep(2.0)
        base, mode0 = nc.sbus_dump(), nc.mode()
        m11, nm = l11.mark(), nc.dev.mark()
        stalls = ([f"#L90,{ms}" for ms in range(110, 491, 25)][:16] + ["save"] * 4) if hooks else ["save"] * 20
        try:
            nc.set_debug_flags(FLAGS_ALL | (DBG_WIRE if hooks else 0))
            nc.ack({"type": "START_MONITOR"})
            for s in stalls:
                if s == "save":
                    nc.set_config({"chRateHz": g.before.get("chRateHz", 5)})
                    time.sleep(0.6)
                else:
                    nc.dev.send(s)
                    time.sleep(int(s.split(",")[1]) / 1000 + 0.6)
        finally:
            nc.ack({"type": "STOP_MONITOR"})
            nc.set_debug_flags(0)
        # #L09's fps is the last whole second loop() timed (see sbus.lock_under_load): read 1 s after the last stall it
        # still counted the stall's lost frames (fps 81, 20261009-082225). Read it once a clean second has passed.
        time.sleep(FPS_WINDOW_CLEAR_S)
        lines = [x.rstrip() for x in nc.dev.since(nm)]
        after = nc.sbus_dump()
        phantom = _frames(l11, m11, dev, set(chans))
        if phantom:
            problems.append(f"detector knobs sent {len(phantom)} frame(s) after the stalls, first {phantom[:3]}: a "
                            f"phantom SBUS frame was decoded")
        trig = [x for x in lines if '"type":"rc_trig"' in x]
        if trig:
            problems.append(f"{len(trig)} rc_trig line(s) with every band inert: {trig[:2]}")
        disp = [x for x in lines if x.startswith("[DISPATCH]") and "seeded" not in x and "SetSpeed" not in x
                and "SetAccel" not in x]
        if disp:
            problems.append(f"{len(disp)} [DISPATCH] line(s) during the stalls, first {disp[:2]}")
        held = f'"channels":[{",".join(map(str, base["channels"]))}]'
        exc = [x for x in lines if x.startswith('{"type":"PWM_UPDATE"') and held not in x]
        if exc:
            problems.append(f"{len(exc)} monitor frame(s) whose channels differ from the still inputs")
        if nc.mode() != mode0:
            problems.append(f"the mode moved from {mode0} to {nc.mode()}")
        if after["fps"] < SBUS_FULL_FPS or after["variant"] != base["variant"]:
            problems.append(f"after the stalls: fps {after['fps']}, {after['variant']}")
        if hooks and sum(1 for x in lines if x.startswith("[HIL] #L90: loop() resumed")) < 10:
            problems.append("fewer than 10 '#L90 ... resumed' lines: the stalls did not all happen")
        notes.append(f"{len(stalls)} stalls ({'#L90 and saves' if hooks else 'saves'}); detectors {dets[:len(watch)]} on "
                     f"CH{watch} -> slot {slot} ch {chans}; knobs off meanwhile {off}")
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ saves while inputs are live
@test("sbus.reconfig_live", "A config save acts on nothing by itself: a switch parked on a tier does not fire on a USB "
      "or a bridged save (and still fires on its next move); a matrix button held across a USB or a bridged save "
      "fires its tap at most once", needs=["sbus", "navicore", "wcb1"], links=["W1S2"])
def reconfig_live(bench):
    """Every apply re-seeds the switches without firing (g_switchSeedPending, rc_config.h:1920; processSwitches,
    NaviCore.ino:2428-2451); the USB SET_CONFIG also re-arms the matrix debounce (:3966-3975) and the bridged one does
    not (rc_telemetry.h:1120-1200, D-NC44). A switch on the rx stick with a marker per position and a guarded marker
    mapping on an unmapped matrix button's slot, inside nc_guard. Whether the held button's parked tap should fire at
    all is sbus.reconfig_parked_tap_cleared's question (D-NC44); here it must never fire twice. The bridged save is one
    packet: '{"type":"SET_CONFIG","saveId":N,"data":{"chRateHz":<as saved>}}'."""
    l12 = link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    sticks = Sticks(ctl, cfg)
    w1 = usb_wcb(bench)
    mode = nc.mode()
    (ib, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        tap_ms, rate = g.before.get("tapWindowMs", 500), g.before.get("chRateHz", 5)
        free = _free_switches(g.before)
        if not free:
            raise Skip("no free switch to rebind")
        label = free[0][1]
        tags = {p: marker(f"R{p}") for p in range(3)}
        btag = marker("RB")
        nc.set_config({"switches": {label: {"channel": ch, "positions": 3, **_sw_tiers(tags)}},
                       "mappings": {str(mode * 100 + slot): {"exclusive": False, "t1": [_act(btag)]}}})
        time.sleep(1.0)

        def usb_save():
            nc.set_config({"chRateHz": rate})

        def mesh_save():
            r = bridged(w1, {"type": "SET_CONFIG", "saveId": 4401, "data": {"chRateHz": rate}},
                        r'"type":"ACK","of":"SET_CONFIG".*"ok":true', timeout=5.0)
            if r.match is None:
                problems.append("the bridged SET_CONFIG got no ok ACK")
        try:
            pm = l12.mark()
            sticks.set("rx", 1600)
            _wait_all(l12, pm, [tags[2]], 2.0)
            for what, save in (("USB", usb_save), ("bridged", mesh_save)):
                pm = l12.mark()
                save()
                time.sleep(1.5)
                if l12.received(pm):
                    problems.append(f"a {what} save with the switch parked on position 2 fired {l12.received(pm)!r}")
            pm = l12.mark()
            sticks.set("rx", 450)
            if _count(_wait_all(l12, pm, [tags[0]], 2.0), tags[0]) != 1:
                problems.append("after the saves the switch did not fire position 0 once")
            sticks.set("rx", SBUS_CENTER)
            time.sleep(1.0)
            for what, save in (("USB", usb_save), ("bridged", mesh_save)):
                pm, nm = l12.mark(), nc.dev.mark()
                ctl.button(ib, True)
                time.sleep(0.3)
                save()
                time.sleep(0.3)
                ctl.button(ib, False)
                time.sleep(tap_ms / 1000 + 1.2)
                n_trig, n_mark = len(_taps(nc, nm, mode, slot)), _count(l12.received(pm), btag)
                notes.append(f"held across a {what} save: {n_trig} rc_trig, {n_mark} marker")
                if n_trig > 1 or n_mark > 1:
                    problems.append(f"a button held across a {what} save fired {n_trig} times ({n_mark} markers)")
            pm, nm = l12.mark(), nc.dev.mark()
            _press(ctl, ib, 0.12)
            time.sleep(tap_ms / 1000 + 1.2)
            if (len(_taps(nc, nm, mode, slot)), _count(l12.received(pm), btag)) != (1, 1):
                problems.append("a fresh press after the saves did not fire exactly once")
        finally:
            ctl.button(ib, False)
            sticks.center()
            time.sleep(tap_ms / 1000 + 0.5)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("sbus.reconfig_parked_tap_cleared", "(should) A config save forgets a tap still parked on a held matrix button: "
      "releasing it after a save that changed that button's mapping fires nothing, neither the old mapping nor the new",
      needs=["sbus", "navicore", "wcb1"], links=["W1S2"])
def reconfig_parked_tap_cleared(bench):
    """NAVICORE.md D-NC44. No config apply clears tapState: a first press is parked while held (checkDeferredTap,
    NaviCore.ino:2404-2426), the USB SET_CONFIG resets only the matrix debounce (:3966-3975), and the release re-times
    the parked tap (rcMatrixRelease :2379-2402), which rcDispatch then runs against the mapping as it is NOW
    (:2286-2307). So a press made under one mapping fires another, and a save's own leftovers can act; nc_guard's
    RESET_DEFAULTS rung is exposed to it (s40 _defaults_live_effects). Recommendation: every config apply, on either
    transport, clears tapState and re-arms the matrix. A guarded mapping on an unmapped button's slot: t1 = marker A,
    changed to marker B while the button is held."""
    l12 = link(bench, 1, "S2")
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    (ib, _, slot), = _unmapped_buttons(nc, cfg, ncfg, [mode], 1)
    with nc_guard(bench) as g:
        nc = g.nc
        tap_ms = g.before.get("tapWindowMs", 500)
        a, b = marker("PA"), marker("PB")
        key = str(mode * 100 + slot)
        nc.set_config({"mappings": {key: {"exclusive": False, "t1": [_act(a)]}}})
        time.sleep(0.5)
        try:
            pm, nm = l12.mark(), nc.dev.mark()
            ctl.button(ib, True)
            time.sleep(0.3)
            nc.set_config({"mappings": {key: {"exclusive": False, "t1": [_act(b)]}}})
            time.sleep(0.3)
            ctl.button(ib, False)
            time.sleep(tap_ms / 1000 + 1.2)
            trigs, got = _taps(nc, nm, mode, slot), l12.received(pm)
        finally:
            ctl.button(ib, False)
            time.sleep(0.3)
    fired = [x for x, t in (("the old mapping", a), ("the new mapping", b)) if _count(got, t)]
    assert not trigs and not fired, (f"the tap parked across the save fired on release: rc_trig {[t for _, t in trigs]}, "
                                     f"{', '.join(fired) or 'no marker'}")
