"""NaviCore's RC engine driven through its USB JSON (TRIGGER, TEST_ACTION, CALIB) and the mesh: tiers, delays, the
calibration and skip-if-running gates, rc_trig, the mode and stats reports (docs/hil_plan/NAVICORE.md NC-WP4, ids
ncengine.*). The SBUS-driven half of the engine is s42 (NC-WP5), which takes its helpers from here.

How the tests see the engine work. A guarded mapping (hil/nc_guard.py, D-NC2) on an INERT key - a matrix slot whose band
is 0/0, which pwmToButton never decodes from SBUS (NaviCore.ino:580-591), in a mode where the slot has no mapping (s40's
_inert_keys; 136 first on this bench) - carries wcb_unicast actions to W1 whose command is ';S2HIL<tag><nonce>'. W1
runs it and writes 'HIL<tag><nonce>' + CR out of its S2, where probe1 records it on its own clock (the W1S2 wire,
9600 baud). So every tier, delay and gate shows up as bytes on a probe wire, byte-exact and timestamped, and nothing
moves: a USB TRIGGER names its mode explicitly, so an inert key fires only when a test triggers it.
- Order and timing are read on the probe clock (Link.time_of), relative to a marker of the same trigger, so USB and
  mesh latency cancel. A delayed marker may land up to EARLY_MS early (its reference was the one held up) or LATE_MS
  late (an ETM retry, W1's queue).
- The [DISPATCH] lines (SET_DEBUG_FLAGS, NaviCore.ino:494-508) are NaviCore's own trace of what it sent, in order. They
  go through vlogf, which drops a line when the USB TX ring is short, and a lone line can sit unsent until more output
  follows it, so they are read after a '#L12' flush (s40 _flushed).
- rc_trig is emitted by rcDispatch BEFORE any action runs and before the calibration gate (NaviCore.ino:2225-2285): on
  USB (dropped, not queued, when the TX ring is short) and as a best-effort mesh broadcast that W1 prints only inside
  its 20 s relay window (hil/ncmesh.py).
Bench facts relied on (NAVICORE.md §1.4): NaviCore is WCB 20; W1 has probe wires on S1 and S2; slot 4 is a remote
Maestro slot (type 2, device 4) that nothing hosts, so its Pololu frames reach W1's S1 probe (W1 is Maestro_Remote and
writes a broadcast Kyber chunk to its local Maestro port, WCB.ino:5626-5676) and move nothing; Maestro 2 is hosted by
W2; NaviCore's own Maestro 1 (slot 1, local) is the dome, and the one test here that moves it is listed in
hil/servos.py, as is the one that changes the mode (mode-aware knobs re-dispatch); modeReport goes to W2 and statsReport
to W1.
"""
import re
import time

from hil.nc_guard import nc_guard
from hil.navicore import DBG_DFP, DBG_MAESTRO, DBG_MP3, DBG_SERIAL, DBG_WCB, DBG_WLED, NaviCore
from hil.ncmesh import bridged
from hil.runner import Skip, test
from suites.common import link, marker, require_tokens, usb_wcb
from suites.s40_navicore_config import HOOK_SKIP, _flushed, _hooks, _inert_keys

EARLY_MS = 100          # a delayed marker may beat its nominal delay by this much on the probe clock
LATE_MS = 400           # ...or trail it by this much
AT_ONCE_MS = 400        # the markers of one dispatch pass land within this of each other
WARN_FULL = re.compile(r"^WARN: pendingActions full \(8 slots\)")        # scheduleAction, NaviCore.ino:2029-2040
REMOTE_TRIGGER_QUEUE = 8                                                 # xQueueCreate(8, ...) (NaviCore.ino:4842)
STALL_MS = 4000                                                          # the #L90 stall mesh_trigger_burst uses
FLAGS_ALL = DBG_MAESTRO | DBG_WCB | DBG_WLED | DBG_MP3 | DBG_SERIAL | DBG_DFP


# ------------------------------------------------------------------ helpers (s42 imports these)
def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _act(text, delay=0, target=1, port="S2"):
    """A wcb_unicast action whose effect is `text` + CR out of W<target>'s <port> (a probe's wire)."""
    a = {"type": "wcb_unicast", "target": str(target), "cmd": f";{port}{text}"}
    if delay:
        a["delay"] = int(delay)
    return a


def _wait_all(l, since, texts, timeout=3.0):
    """The bytes Link `l` received since `since`, once every text of `texts` (+ CR) is among them or `timeout` passed."""
    want = [t.encode() + b"\r" for t in texts]
    deadline = time.monotonic() + timeout
    while True:
        got = l.received(since)
        if all(w in got for w in want) or time.monotonic() >= deadline:
            return got
        time.sleep(0.05)


def _count(got, text):
    return got.count(text.encode() + b"\r")


def _probe_ms(l, since, data, nth=0):
    """Probe millis() of the burst that carried the first byte of the nth (0-based) occurrence of `data` (str or bytes)
    in Link `l`'s stream since `since`, or None (Probe.time_of, for any occurrence)."""
    data = data.encode() if isinstance(data, str) else data
    stream, starts = b"", []
    for ms, chunk in l.bursts(since):
        starts.append((len(stream), ms))
        stream += chunk
    i = -1
    for _ in range(nth + 1):
        i = stream.find(data, i + 1)
        if i < 0:
            return None
    return [ms for start, ms in starts if start <= i][-1]


def _host_time(l, since, data):
    """Host monotonic time of the probe line that completed `data` (bytes) in Link `l`'s stream since `since`, or None.
    The probe reports each burst as 'RX <ch> <ms> <hex>' (hil/probe.py bursts) and the host stamps the line as it
    arrives, a few ms after the bytes, so an interval measured from a host-side send to this is an upper bound on what
    the boards took, and its lower bound a true minimum."""
    l.received(since)                       # checks the channel has been this wire's since the mark
    ch, stream = l.channel, b""
    for ts, text in list(l.probe.dev.lines[since:]):
        m = re.match(rf"^RX {ch} \d+ ([0-9A-F]*)$", text)
        if m:
            stream += bytes.fromhex(m.group(1))
            if data in stream:
                return ts
    return None


def _dispatch_lines(lines, prefix):
    return [x for x in lines if x.startswith(prefix)]


def _rc_trigs(nc, since, keys=None):
    """[(host time, (mode, btn, tap))] of the USB rc_trig lines since mark `since`, optionally only those whose (mode,
    btn) is in `keys`."""
    out = []
    for ts, o in nc.rc_events(since):
        k = (o.get("mode"), o.get("btn"), o.get("tap"))
        if keys is None or k[:2] in keys:
            out.append((ts, k))
    return out


def _engine_inert(nc, before):
    """SET_CONFIG that leaves the SBUS side of the engine unable to act, for a test that stalls loop() long enough to
    overflow the SBUS UART: about 100 ms (Serial1 keeps the core's 256-byte RX ring, HardwareSerial.cpp:123 in core
    3.3.4, since NaviCore sets only its TX buffer, NaviCore.ino:4658; plus the 128-byte FIFO; SBUS-24 brings 4
    bytes/ms). After an overflow the reader may decode one misaligned frame on a
    stream it has already locked (sbus_reader.h, the eager length flush; the plan's nc.sbus.overflow_misalign
    hypothesis, which sbus.stall_no_phantom measures), and that frame's channels are garbage: a matrix press on a real
    mapping, a mode change, a knob twitch. So, inside the caller's nc_guard: every band 0/0 (pwmToButton decodes
    nothing), no mode switch (funcBindings.mode -1: readBoundSwitchSbus answers -1, NaviCore.ino:573-578, and the mode
    block never runs), and every knob that has a function set to function 0 (processKnobs skips it, :2607-2608). A switch
    tier needs switchSettleMs of one steady position, which a one-frame blip never gives. Each knob is sent whole: a
    knob named in SET_CONFIG takes its default for every key it omits (rc_config.h:1645-1668). -> the knob labels
    turned off."""
    bands = [dict(t, minPwm=0, maxPwm=0) for t in before.get("thresholds") or []]
    knobs = {lab: dict(k, function=0) for lab, k in (before.get("knobs") or {}).items() if k.get("function")}
    change = {"thresholds": bands, "funcBindings": {"mode": -1}}
    if knobs:
        change["knobs"] = knobs
    nc.set_config(change)
    return sorted(knobs)


def _remote_slot(cfg, prefer=4):
    """(slot, device) of a remote (type 2) Maestro slot whose device is 3-8, which no board hosts on this bench: slot 4
    first. Its frames go into the broadcast WCBStream (maestroWrite, NaviCore.ino:647-701) and every Maestro_Remote WCB
    writes them to its local Maestro port, where no Maestro has that number (NAVICORE.md §1.2). Skip unless one
    exists."""
    maes = cfg.get("maestros") or []
    for slot in [prefer] + [s for s in range(3, 9) if s != prefer]:
        if slot <= len(maes) and maes[slot - 1].get("type") == 2 and 3 <= int(maes[slot - 1].get("device", 0)) <= 8:
            return slot, int(maes[slot - 1]["device"])
    raise Skip("NaviCore has no remote Maestro slot on device 3-8 (the observation window of NAVICORE.md §1.2)")


def _pololu(dev, cmd, ch, value):
    """A Pololu-protocol frame: AA <device> <cmd & 0x7F> <ch> <value low 7 bits> <value high 7 bits> (maestroWrite with
    maestroSetTarget / SetSpeed / SetAccel, NaviCore.ino:1033-1082)."""
    return bytes([0xAA, dev, cmd & 0x7F, ch, value & 0x7F, (value >> 7) & 0x7F])


def _wire_bytes(lines, port="WCBStream"):
    """The bytes a NAVICORE_HIL_HOOKS image logged under DBG_WIRE for `port` ('[WIRE] <port> <off>/<len>: <hex>',
    navicore_hil.h wire()), joined in order."""
    out = b""
    for x in lines:
        m = re.match(rf"^\[WIRE\] {re.escape(port)} \d+/\d+: ([0-9A-F ]+)$", x.rstrip())
        if m:
            out += bytes.fromhex(m.group(1).replace(" ", ""))
    return out


# ============================================================ tiers
def _fire(nc, l12, mode, btn, tap, want, tags, label):
    """TRIGGER (mode, btn, tap) over USB -> problems. `want` are the tier numbers whose markers must arrive, each once,
    in that order both on the wire and in NaviCore's dispatch trace, within one dispatch pass; every other tier must
    stay silent."""
    pm, nm = l12.mark(), nc.dev.mark()
    ack, trig = nc.trigger(mode, btn, tap)
    _wait_all(l12, pm, [tags[t] for t in want], 3.0)
    time.sleep(0.8)                               # room for a tier that should not have fired
    got = l12.received(pm)
    lines = _flushed(nc, nm, settle=0.1)
    where = f"{label} tap {tap}"
    problems = []
    if not ack.get("ok"):
        problems.append(f"{where}: ACK {ack}")
    if trig is None:
        problems.append(f"{where}: no rc_trig on USB")
    missing = [t for t in want if _count(got, tags[t]) != 1]
    extra = [t for t in tags if t not in want and _count(got, tags[t])]
    if missing:
        problems.append(f"{where}: tiers {missing} did not arrive exactly once on W1 S2")
    if extra:
        problems.append(f"{where}: tiers {extra} fired as well")
    if want and not missing:
        pos = [got.find(tags[t].encode() + b"\r") for t in want]
        if pos != sorted(pos):
            problems.append(f"{where}: the wire order was tiers {[t for _, t in sorted(zip(pos, want))]}, not {want}")
        ms = [_probe_ms(l12, pm, tags[t]) for t in want]
        if None not in ms and max(ms) - min(ms) > AT_ONCE_MS:
            problems.append(f"{where}: the tiers arrived over {max(ms) - min(ms)} ms; a USB TRIGGER fires them in one "
                            f"pass")
    fired = [t for x in _dispatch_lines(lines, "[DISPATCH] WCB→1  ;S2") for t in tags if x.endswith(tags[t])]
    if fired != list(want):
        problems.append(f"{where}: NaviCore's dispatch trace fired tiers {fired}, expected {list(want)}")
    return problems


@test("ncengine.tiers_exclusive_cumulative", "A USB TRIGGER bypasses the tap engine: tap N on a non-exclusive mapping "
      "fires tiers 1..N at once, in order on W1 S2; an exclusive mapping fires tier N alone; tap 4 (the long press) "
      "fires tier 4 alone either way, and nothing when tier 4 is empty", needs=["navicore", "wcb1"], links=["W1S2"])
def tiers_exclusive_cumulative(bench):
    """rcDispatch (NaviCore.ino:2225-2307): 'exclusive || tapCount == RC_TAP_LONG' runs mapping.t[tap-1] only; otherwise
    every tier up to the matched one, t1 first. A USB TRIGGER calls rcDispatch at once (NaviCore.ino:4051-4061), so the
    SBUS path's tap window plays no part here (that timing is s42's sbus.long_press_configured and
    sbus.other_button_commits). The mapping is on an inert key inside nc_guard, one W1 S2 marker per tier. The wire
    order and NaviCore's [DISPATCH] trace must both read t1, t2, ...: two views of one pass."""
    l12 = link(bench, 1, "S2")
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        key = _inert_keys(g.before, 1)[0]
        mode, btn = divmod(int(key), 100)
        tags = {t: marker(f"X{t}") for t in (1, 2, 3, 4)}

        def mapping(exclusive, tiers=(1, 2, 3, 4)):
            m = {"exclusive": exclusive}
            for t in tiers:
                m[f"t{t}"] = [_act(tags[t])]
            return m
        bench.note(f"mapping {key} (mode {mode}, slot {btn}), one W1 S2 marker per tier")
        with nc.debug(DBG_WCB):
            nc.set_config({"mappings": {key: mapping(False)}})
            for tap, want in ((1, [1]), (2, [1, 2]), (3, [1, 2, 3]), (4, [4])):
                problems += _fire(nc, l12, mode, btn, tap, want, tags, "cumulative")
            nc.set_config({"mappings": {key: mapping(True)}})
            for tap, want in ((1, [1]), (2, [2]), (3, [3]), (4, [4])):
                problems += _fire(nc, l12, mode, btn, tap, want, tags, "exclusive")
            nc.set_config({"mappings": {key: mapping(False, (1, 2, 3))}})
            problems += _fire(nc, l12, mode, btn, 4, [], tags, "no tier 4")
    assert not problems, "; ".join(problems)


# ============================================================ delays
@test("ncengine.delay_queue", "Delayed actions run in parallel from the trigger on the probe clock (two 1000 ms delays "
      "land together, 2000 ms after); with 10 delayed actions the 8 pending slots fill and the 9th and 10th fire at once, "
      "each with 'WARN: pendingActions full (8 slots)'; a delayed copy still fires after a SET_CONFIG removes its "
      "mapping", needs=["navicore", "wcb1"], links=["W1S2"])
def delay_queue(bench):
    """scheduleAction (NaviCore.ino:2029-2040) copies the action into one of PENDING_ACTION_SLOTS (8) with fireAt =
    now + delay, or, with every slot busy, prints the WARN and runs it now with its delay dropped; checkPendingActions
    (:2202-2220) runs each when due from its own copy, so a SET_CONFIG that removes the mapping cannot recall it. Part 1
    measures three delays against an undelayed marker of the same tier. Part 2 fills the queue: tap 2 on a
    non-exclusive mapping schedules t1's five actions (1000-1400 ms) then t2's five (1500-1900 ms), so t2's 4th and 5th
    find it full; they are the time reference, and the eight others must land at their own delays. Part 3 removes the
    mapping while its 2500 ms action is pending (the save stalls loop(), hence the extra slack). Everything is marker
    bytes on W1 S2; the mappings sit on inert keys inside nc_guard."""
    l12 = link(bench, 1, "S2")
    problems, notes = [], []

    def late(ref, t, nominal, where, slack=0):
        if ref is None or t is None:
            problems.append(f"{where}: marker missing on the probe clock")
            return
        d = t - ref
        notes.append(f"{where} {d} ms")
        if not nominal - EARLY_MS <= d <= nominal + LATE_MS + slack:
            problems.append(f"{where}: {d} ms after its reference, expected {nominal} ms")

    with nc_guard(bench) as g:
        nc = g.nc
        k1, k2, k3 = _inert_keys(g.before, 3)
        z, a, b, c = (marker(x) for x in ("DZ", "DA", "DB", "DC"))
        p = [marker(f"P{i}") for i in range(5)]
        q = [marker(f"Q{i}") for i in range(5)]
        y, x = marker("DY"), marker("DX")
        nc.set_config({"mappings": {
            k1: {"exclusive": False, "t1": [_act(z), _act(a, 1000), _act(b, 1000), _act(c, 2000)]},
            k2: {"exclusive": False, "t1": [_act(p[i], 1000 + 100 * i) for i in range(5)],
                 "t2": [_act(q[i], 1500 + 100 * i) for i in range(5)]},
            k3: {"exclusive": False, "t1": [_act(y), _act(x, 2500)]}}})
        # Part 1: parallel delays
        mode, btn = divmod(int(k1), 100)
        pm = l12.mark()
        nc.trigger(mode, btn, 1)
        _wait_all(l12, pm, [z, a, b, c], 5.0)
        tz = _probe_ms(l12, pm, z)
        late(tz, _probe_ms(l12, pm, a), 1000, "delay 1000 (first)")
        late(tz, _probe_ms(l12, pm, b), 1000, "delay 1000 (second, in parallel)")
        late(tz, _probe_ms(l12, pm, c), 2000, "delay 2000")
        time.sleep(0.5)
        # Part 2: ten delayed actions, eight slots
        mode, btn = divmod(int(k2), 100)
        pm, nm = l12.mark(), nc.dev.mark()
        nc.trigger(mode, btn, 2)
        got = _wait_all(l12, pm, p + q, 5.0)
        lines = _flushed(nc, nm, settle=0.1)
        missing = [t for t in p + q if _count(got, t) != 1]
        if missing:
            problems.append(f"overflow: {len(missing)} of the 10 delayed markers did not arrive exactly once")
        warns = sum(1 for s in lines if WARN_FULL.match(s))
        if warns != 2:
            problems.append(f"overflow: {warns} 'pendingActions full' WARN lines, expected 2 (the 9th and 10th)")
        ref, tenth = _probe_ms(l12, pm, q[3]), _probe_ms(l12, pm, q[4])
        if ref is not None and tenth is not None and tenth - ref > 250:
            problems.append(f"overflow: the 10th landed {tenth - ref} ms after the 9th; both fire at once when the "
                            f"queue is full")
        for i in range(5):
            late(ref, _probe_ms(l12, pm, p[i]), 1000 + 100 * i, f"t1 action {i + 1} (delay {1000 + 100 * i})")
        for i in range(3):
            late(ref, _probe_ms(l12, pm, q[i]), 1500 + 100 * i, f"t2 action {i + 1} (delay {1500 + 100 * i})")
        time.sleep(0.5)
        # Part 3: the mapping goes, its pending copy stays
        mode, btn = divmod(int(k3), 100)
        pm = l12.mark()
        nc.trigger(mode, btn, 1)
        l12.expect(y.encode() + b"\r", timeout=3, since=pm)
        nc.set_config({"mappings": {k3: {}}})
        if k3 in (nc.config().get("mappings") or {}):
            problems.append(f"mapping {k3} was not removed by the SET_CONFIG")
        _wait_all(l12, pm, [x], 6.0)
        late(_probe_ms(l12, pm, y), _probe_ms(l12, pm, x), 2500, "the removed mapping's delayed copy", slack=700)
    bench.note("probe-clock delays: " + "; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ the calibration gate
@test("ncengine.calibration_gate", "CALIB mutes action dispatch but not rc_trig; TEST_ACTION still sends; a delayed "
      "action that comes due under CALIB is dropped, not deferred; PING and STOP_MONITOR each end CALIB; a CALIB "
      "bridged over the mesh mutes nothing", needs=["navicore", "wcb1"], links=["W1S2"])
def calibration_gate(bench):
    """calibrationActive gates rcExecuteAction before anything is scheduled (NaviCore.ino:2135-2150), and
    checkPendingActions when a delayed action comes due, where the slot is freed either way (:2202-2220); rcDispatch
    emits rc_trig before either (:2225-2285); rcTestAction bypasses the gate (:2155-2173). PING and STOP_MONITOR clear
    it (:3855-3858, :3988-3993); the mesh handler accepts CALIB and does nothing (rc_telemetry.h:2390-2403). The mapping
    (t1 an undelayed marker, t2 a 1500 ms one) sits on an inert key in nc_guard, whose restore also PINGs."""
    l12 = link(bench, 1, "S2")
    w1 = usb_wcb(bench)
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        key = _inert_keys(g.before, 1)[0]
        mode, btn = divmod(int(key), 100)
        a, d, t = marker("CA"), marker("CD"), marker("CT")
        nc.set_config({"mappings": {key: {"exclusive": False, "t1": [_act(a)], "t2": [_act(d, 1500)]}}})
        try:
            with nc.debug(DBG_WCB):
                # 1. under CALIB: rc_trig, but no dispatch and no bytes
                cm = nc.dev.mark()
                if not nc.calib(True).get("ok") or not any("SUPPRESSED" in s for s in _flushed(nc, cm, 0.1)):
                    problems.append("CALIB on: no ACK or no '[CALIB] action dispatch SUPPRESSED' line")
                pm, nm = l12.mark(), nc.dev.mark()
                _, trig = nc.trigger(mode, btn, 1)
                time.sleep(1.5)
                lines = _flushed(nc, nm, 0.1)
                if trig is None:
                    problems.append("under CALIB the trigger emitted no rc_trig")
                if l12.received(pm):
                    problems.append("under CALIB the trigger's action reached W1 S2")
                if _dispatch_lines(lines, "[DISPATCH] WCB"):
                    problems.append("under CALIB NaviCore printed a [DISPATCH] WCB line")
                # 2. TEST_ACTION is not gated
                pm = l12.mark()
                if not nc.test_action(_act(t)).get("ok"):
                    problems.append("TEST_ACTION under CALIB was refused")
                if t.encode() + b"\r" not in _wait_all(l12, pm, [t], 3.0):
                    problems.append("TEST_ACTION under CALIB did not reach W1 S2")
                # 3. PING ends it
                nc.ping()
                pm = l12.mark()
                nc.trigger(mode, btn, 1)
                if a.encode() + b"\r" not in _wait_all(l12, pm, [a], 3.0):
                    problems.append("after PING the trigger still dispatched nothing")
                time.sleep(0.8)
                # 4. a delayed action that comes due under CALIB is dropped
                pm = l12.mark()
                t0 = time.monotonic()
                nc.trigger(mode, btn, 2)
                l12.expect(a.encode() + b"\r", timeout=3, since=pm)
                nc.calib(True)
                armed = time.monotonic() - t0
                time.sleep(2.5)
                if armed > 1.2:
                    problems.append(f"CALIB went on {armed:.2f} s after the trigger, too late for the 1500 ms action")
                elif d.encode() in l12.received(pm):
                    problems.append("a delayed action that came due under CALIB still fired")
                # 5. STOP_MONITOR ends it; the dropped action stays dropped
                nc.ack({"type": "STOP_MONITOR"})
                time.sleep(2.0)
                if d.encode() in l12.received(pm):
                    problems.append("the delayed action dropped under CALIB fired after CALIB ended")
                pm = l12.mark()
                nc.trigger(mode, btn, 1)
                if a.encode() + b"\r" not in _wait_all(l12, pm, [a], 3.0):
                    problems.append("after STOP_MONITOR the trigger still dispatched nothing")
                time.sleep(0.5)
                # 6. CALIB over the mesh changes nothing
                bridged(w1, {"type": "CALIB", "on": True}, None, timeout=1.0)
                pm = l12.mark()
                nc.trigger(mode, btn, 1)
                if a.encode() + b"\r" not in _wait_all(l12, pm, [a], 3.0):
                    problems.append("a CALIB bridged over the mesh muted the USB trigger")
        finally:
            nc.calib(False)
    assert not problems, "; ".join(problems)


# ============================================================ rc_trig
@test("ncengine.rc_trig_emit", "Every dispatch emits one rc_trig, on USB and relayed by W1 as the same line: for an "
      "empty tier, for tap 4, under CALIB, and for a TRIGGER that came over the mesh", needs=["navicore", "wcb1"],
      links=[])
def rc_trig_emit(bench):
    """rcDispatch writes {"sys":1,"type":"rc_trig","id",mode,btn,tap} to USB and broadcasts the same text best-effort
    (rcTelemetry::emitTrig, rc_telemetry.h:1735-1745) before it looks at the mapping or the calibration gate
    (NaviCore.ino:2225-2285), so an unmapped slot (an inert key: its tiers are empty) and a calibrating board still
    report it. W1 prints NaviCore's broadcasts only inside its 20 s relay window, opened with a bridged PING. The USB
    line is dropped only when the TX ring is short (not expected on an idle console); the mesh copy is a broadcast with
    no ACK, so each copy that arrives must equal the USB line, at least one must arrive, and none twice. Nothing runs:
    the key has no mapping."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    nid = nc.wcb_status()["self"]
    key = _inert_keys(nc.config(), 1)[0]
    mode, btn = divmod(int(key), 100)
    problems, relayed = [], 0

    def line(tap):
        return f'{{"sys":1,"type":"rc_trig","id":{nid},"mode":{mode},"btn":{btn},"tap":{tap}}}'

    def check(label, tap, since_nc, since_w1):
        nonlocal relayed
        time.sleep(1.0)
        usb = [x.rstrip() for x in nc.dev.since(since_nc) if '"type":"rc_trig"' in x]
        mesh = [x.rstrip() for x in w1.dev.since(since_w1) if '"type":"rc_trig"' in x]
        if usb != [line(tap)]:
            problems.append(f"{label}: USB rc_trig lines {usb}, expected [{line(tap)}]")
        if len(mesh) > 1 or any(m != line(tap) for m in mesh):
            problems.append(f"{label}: W1 relayed {mesh}")
        relayed += len(mesh) == 1

    if bridged(w1, {"type": "PING"}, r'"type":"PONG"', timeout=3.0).match is None:
        raise Skip("no bridged PONG from NaviCore: W1's relay window cannot be opened")
    try:
        for label, tap, calib in (("empty tier", 1, False), ("tap 4", 4, False), ("under CALIB", 2, True)):
            if calib:
                nc.calib(True)
            nm, wm = nc.dev.mark(), w1.dev.mark()
            ack, _ = nc.trigger(mode, btn, tap)
            if ack != {"type": "ACK", "ok": True}:
                problems.append(f"{label}: ACK {ack}")
            check(label, tap, nm, wm)
            if calib:
                nc.calib(False)
        nm, wm = nc.dev.mark(), w1.dev.mark()
        w1.send(f';W{nid},{{"type":"TRIGGER","mode":{mode},"btn":{btn},"tap":3}}')
        time.sleep(0.5)
        check("mesh TRIGGER", 3, nm, wm)
    finally:
        nc.calib(False)
    bench.note(f"W1 relayed {relayed} of 4 rc_trig broadcasts (best effort)")
    if not relayed:
        problems.append("W1 relayed none of the 4 rc_trig broadcasts inside its relay window")
    assert not problems, "; ".join(problems)


@test("ncengine.mesh_trigger_burst", "Mesh TRIGGERs that land while loop() is busy wait in an 8-deep queue: of 12 sent "
      "during a 4 s stall exactly 8 dispatch, in the order sent, once loop() resumes; the rest are dropped silently "
      "(NAVICORE_HIL_HOOKS build only)", needs=["navicore", "wcb1"], links=[])
def mesh_trigger_burst(bench):
    """A bridged TRIGGER is parsed on the WiFi task and queued (queueRemoteTrigger, NaviCore.ino:2989-2994: xQueueSend
    with no wait into an 8-item queue, :4842); loop() drains every queued trigger per pass (drainRemoteTriggers,
    :5370-5380). The receive path ACKs and delivers on Core 0 (WCB_Client.cpp:2818-2903), so a stalled loop() still
    fills the queue, and the 9th trigger on is dropped with no log line: the pinned behaviour (the queue's comment calls
    it 'drop under load'). The stall is INF9's '#L90,<ms>'; it outlasts the SBUS UART's buffer, so the SBUS side of the
    engine is made inert for the test (_engine_inert, in nc_guard). The 12 triggers use 12 inert keys (no mapping), so
    each dispatch is one rc_trig and nothing else. The burst must leave W1 well inside the stall, or the test skips."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    if not _hooks(nc):
        raise Skip(HOOK_SKIP)
    nid = nc.wcb_status()["self"]
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        keys = [divmod(int(k), 100) for k in _inert_keys(g.before, 12)]
        off = _engine_inert(nc, g.before)
        time.sleep(1.0)
        nm = nc.dev.mark()
        t_stall = time.monotonic()
        nc.dev.send(f"#L90,{STALL_MS}")
        time.sleep(0.15)                          # the stall starts at the top of NaviCore's next loop() pass
        for mode, btn in keys:
            w1.send(f';W{nid},{{"type":"TRIGGER","mode":{mode},"btn":{btn},"tap":1}}')
        sent = time.monotonic() - t_stall
        nc.dev.expect(r"\[HIL\] #L90: loop\(\) resumed after \d+ ms", timeout=STALL_MS / 1000 + 5, since=nm)
        time.sleep(1.5)
        got = [k[:2] for _, k in _rc_trigs(nc, nm, set(keys))]
        bench.note(f"12 mesh TRIGGERs sent {sent:.2f} s into a {STALL_MS} ms stall (knobs off for it: {off}); "
                   f"{len(got)} dispatched: {[keys.index(k) + 1 for k in got]}")
        if sent > STALL_MS / 1000 - 1.5:
            raise Skip(f"W1 took {sent:.1f} s to send the burst: it may not all have landed inside the stall")
        order = [keys.index(k) for k in got]
        if len(got) != REMOTE_TRIGGER_QUEUE:
            problems.append(f"{len(got)} of 12 triggers dispatched after the stall, expected {REMOTE_TRIGGER_QUEUE} "
                            f"(the queue's depth)")
        if order != sorted(order) or len(set(order)) != len(order):
            problems.append(f"the dispatched triggers came in the order {[i + 1 for i in order]}")
        nm = nc.dev.mark()
        mode, btn = keys[-1]
        w1.send(f';W{nid},{{"type":"TRIGGER","mode":{mode},"btn":{btn},"tap":1}}')
        time.sleep(1.5)
        if not _rc_trigs(nc, nm, {(mode, btn)}):
            problems.append("a TRIGGER sent after the burst did not dispatch: the queue did not recover")
    assert not problems, "; ".join(problems)


# ============================================================ TEST_ACTION
def _action_cases(cfg, slot, tag):
    """[(label, action, dispatches, evidence)] for ncengine.test_action_matrix and test_action_skipped_not_ok.
    dispatches: whether the executor sends anything; evidence: the line NaviCore prints for it under FLAGS_ALL (the
    WARN is ungated). The destination cases are added only when the bench config makes them skip (mp3Dest or dfpDest
    off, a WLED id no slot configures)."""
    cases = [("wcb_unicast to W1", {"type": "wcb_unicast", "target": "1", "cmd": f";S2{tag}U"}, True,
              f"[DISPATCH] WCB→1  ;S2{tag}U"),
             ("maestro on remote slot", {"type": "maestro", "target": str(slot), "cmd": "setTarget,5,6000"}, True,
              f"[DISPATCH] Maestro {slot}  setTarget,5,6000"),
             ("maestro on slot 9 (none)", {"type": "maestro", "target": "9", "cmd": "setTarget,5,6000"}, False,
              "WARN: Maestro action with invalid ID 9 (target='9')"),
             ("maestro channel 32", {"type": "maestro", "target": str(slot), "cmd": "setTarget,32,6000"}, False,
              f"[DISPATCH] Maestro {slot}: channel 32 out of range (0-31) — skipped"),
             ("serial to S9", {"type": "serial", "port": "S9", "cmd": f"{tag}S"}, False,
              f"[DISPATCH] Serial TX [S9]  {tag}S")]
    if (cfg.get("mp3Dest") or {}).get("transport") == "off":
        cases.append(("mp3 with mp3Dest off", {"type": "mp3", "fn": 1, "track": 1}, False,
                      "[DISPATCH] MP3 Trigger is disabled in config — action skipped"))
    if (cfg.get("dfpDest") or {}).get("transport") == "off":
        cases.append(("dfplayer with dfpDest off", {"type": "dfplayer", "fn": 1, "chan": 0, "track": 1}, False,
                      "[DISPATCH] DFPlayer is disabled in config — action skipped"))
    ids = {w.get("id") for w in cfg.get("wledSlots") or [] if w.get("configured")}
    wid = next((i for i in range(9, 0, -1) if i not in ids), None)
    if wid:
        cases.append((f"wled id {wid} (not configured)", {"type": "wled", "cmd": f";L{wid},ON"}, False,
                      f"[DISPATCH] WLED {wid} not configured — skipped"))
    return cases


def _run_action(nc, action):
    """TEST_ACTION `action` -> (ACK dict, the console lines it printed, after a '#L12' flush)."""
    nm = nc.dev.mark()
    ack = nc.test_action(action)
    return ack, _flushed(nc, nm, settle=0.3)


@test("ncengine.test_action_matrix", "TEST_ACTION runs each action type through the live executor: a wcb_unicast "
      "reaches W1 S2, a remote-slot Maestro action puts its exact Pololu frame on W1 S1, a bad board id or an unknown "
      "type is refused; an invalid slot, channel 32, a bad serial port, a disabled MP3/DFPlayer and an unconfigured "
      "WLED id are each skipped with their own line and send nothing", needs=["navicore", "wcb1"],
      links=["W1S1", "W1S2"])
def test_action_matrix(bench):
    """rcTestAction parses with the editors' actionFromJson and fires through rcExecuteActionNow with no delay and no
    calibration gate (NaviCore.ino:2155-2173); only a wcb_unicast board id outside 1-20 is refused before that
    (:2164-2168). The executor's skips each print a line (:2047-2130, :937-942, :1776-1780, :1901-1905, :1960-2002).
    Whether a skipped action answers ok is ncengine.test_action_skipped_not_ok's question (D-NC20); here the ok values
    are only noted. The remote slot is one nobody hosts (slot 4 on this bench): its frame reaches W1 S1 through W1's
    Maestro_Remote forward and moves nothing. The serial case names port S9, which matches no aux port, so no byte is
    written, although the trace line is printed first (NaviCore.ino:2093-2100)."""
    l11, l12 = link(bench, 1, "S1"), link(bench, 1, "S2")
    nc = _nc(bench)
    cfg = nc.config()
    slot, dev = _remote_slot(cfg)
    tag = marker("TA")
    problems, oks = [], {}
    with nc.debug(FLAGS_ALL):
        for label, action, dispatches, evidence in _action_cases(cfg, slot, tag):
            m11, m12 = l11.mark(), l12.mark()
            ack, lines = _run_action(nc, action)
            oks[label] = ack.get("ok")
            if evidence not in lines:
                problems.append(f"{label}: no {evidence!r}")
            if label == "wcb_unicast to W1" and f"{tag}U".encode() + b"\r" not in _wait_all(l12, m12, [f"{tag}U"]):
                problems.append(f"{label}: nothing on W1 S2")
            if label == "maestro on remote slot":
                frame = _pololu(dev, 0x84, 5, 6000)
                try:
                    l11.expect(frame, timeout=3, since=m11)
                except AssertionError:
                    problems.append(f"{label}: W1 S1 never got {frame.hex(' ')} (received "
                                    f"{l11.received(m11).hex(' ') or 'nothing'})")
            if label in ("wcb_unicast to W1", "maestro on remote slot") and ack.get("ok") is not True:
                problems.append(f"{label}: ACK {ack}")
            time.sleep(0.4)
            if not dispatches and l11.received(m11):
                problems.append(f"{label}: a skipped action still put {l11.received(m11).hex(' ')} on W1 S1")
        for target in ("0", "21"):
            ack, lines = _run_action(nc, {"type": "wcb_unicast", "target": target, "cmd": f";S2{tag}B"})
            if ack.get("ok") is not False or _dispatch_lines(lines, "[DISPATCH] WCB"):
                problems.append(f"wcb_unicast to board {target}: ACK {ack}, dispatch lines "
                                f"{_dispatch_lines(lines, '[DISPATCH] WCB')}")
        ack, _ = _run_action(nc, {"type": "hilnope"})
        if ack.get("ok") is not False:
            problems.append(f"an unknown action type: ACK {ack}")
    bench.note("TEST_ACTION ok per case: " + ", ".join(f"{k} {v}" for k, v in oks.items()))
    assert not problems, "; ".join(problems)


@test("ncengine.test_action_skipped_not_ok", "(should) TEST_ACTION answers ok:false for an action the executor then "
      "skips: an invalid Maestro slot, channel 32, a bad serial port, a disabled MP3/DFPlayer, an unconfigured WLED id",
      needs=["navicore"], links=[])
def test_action_skipped_not_ok(bench):
    """NAVICORE.md D-NC20. rcTestAction returns true once the action parses and rcExecuteActionNow has run it, whatever
    the executor did with it (NaviCore.ino:2155-2173); the executor's skips return nothing (:2084-2086, :937-942,
    :2093-2100, :1776-1780, :1901-1905, :1992). The config tool's per-action Test button then reports a skipped
    action as sent. Only the wcb_unicast board-id check made it into rcTestAction (:2164-2168, the fix that
    navicore.test_action_bad_target_not_ok pinned). Recommendation: ok:false, with the reason."""
    nc = _nc(bench)
    cfg = nc.config()
    slot, _ = _remote_slot(cfg)
    accepted = []
    with nc.debug(FLAGS_ALL):
        for label, action, dispatches, _ in _action_cases(cfg, slot, marker("TS")):
            if not dispatches and _run_action(nc, action)[0].get("ok") is not False:
                accepted.append(label)
    assert not accepted, f"TEST_ACTION answered ok:true for actions the executor skipped: {accepted}"


@test("ncengine.skip_not_traced_as_sent", "(should) The dispatch trace never reports a send that did not happen: a "
      "serial action to a port NaviCore does not have prints a skip line, not '[DISPATCH] Serial TX [S9] <cmd>'",
      needs=["navicore"], links=[])
def skip_not_traced_as_sent(bench):
    """NAVICORE.md D-NC45. rcExecuteActionNow prints '[DISPATCH] Serial TX [<label or port>]  <cmd>' before it looks at
    the port, and a port other than S3-S5 then writes nothing and says nothing (NaviCore.ino:2093-2100). The Maestro
    case has the same order: '[DISPATCH] Maestro <slot>  <cmd>' is printed before the skip-if-running gate, so a skipped
    action reads as sent until the next line says 'skipped' (:2088-2089; ncengine.maestro_skip_running_slot sees both
    lines). The same kind of trace was fixed for inbound ;M (navicore.maestro_skip_not_logged_as_dispatch). Only the
    serial case is asserted here: the Maestro one needs a moving servo. Recommendation: print the dispatch line after
    the checks, and a skip line (with the reason) otherwise."""
    nc = _nc(bench)
    tag = marker("SK")
    with nc.debug(DBG_SERIAL):
        _, lines = _run_action(nc, {"type": "serial", "port": "S9", "cmd": f"{tag}S"})
    traced = [x for x in lines if x.startswith("[DISPATCH] Serial TX") and x.endswith(f"{tag}S")]
    assert not traced, f"a serial action to port S9, which writes nothing, was traced as sent: {traced}"


# ============================================================ skip-if-running
def _hosted_remote_slot(nc, cfg):
    """(slot, device, host WCB) for a remote (type 2) Maestro slot whose device a WCB on the mesh really hosts: the
    MAESTRO= list of a neighbour's row in NaviCore's own ?WDP,DUMP (WCB_Client WCB_Mgmt.h:237-251). Skip when none."""
    rows = {int(r["N"]): r for r in nc.wdp_dump() if str(r.get("N", "")).isdigit() and r.get("PEER") != "3"}
    for i, m in enumerate(cfg.get("maestros") or []):
        dev = int(m.get("device", 0))
        if m.get("type") != 2 or not 1 <= dev <= 8:
            continue
        for n, r in sorted(rows.items()):
            if n <= 19 and str(dev) in (r.get("MAESTRO") or "-").split("."):
                return i + 1, dev, n
    raise Skip("no remote Maestro slot's device is hosted by a WCB NaviCore hears now")


@test("ncengine.skip_running_fail_open", "skipRunning on a wcb_unicast ;M verb: with no fresh reply cached NaviCore "
      "sends ;M<dev>,getMovingState to the verb's WCB and still sends the verb (fail open); the hosting WCB's :MQR "
      "reply fills the cache, and a verb inside maeGateMs then goes without asking (query verbs only: nothing moves)",
      needs=["navicore", "wcb1"], links=[])
def skip_running_fail_open(bench):
    """maestroVerbBusy (NaviCore.ino:917-935): the device comes from the verb (maeVerbDeviceId, :903-915) and must be a
    remote slot's; a cache entry older than maeGateMs is stale, so it unicasts ';M<dev>,getMovingState' to the same
    WCB, logs 'via-WCB gate: cache stale ... fail-open' and lets the verb go. The hosting WCB rewrites an inbound get to
    reply home (maestroRewriteInboundGet, WCB_Maestro.cpp:664-679) as ':MQR,<dev>,0,MOV,<0|1>' (handleMaestroGet,
    :583-627), which fills the cache (maeConsumeRemoteReply, NaviCore.ino:808-827) and prints '[MAE:<slot>]'. A second
    gated verb inside maeGateMs reads the cache instead. The plan named device 4 on W1, which nothing hosts: its
    warm-up is never answered, so the cached half could not be shown. This uses the remote slot whose device a WCB
    hosts (Maestro 2 on W2 here) and query verbs (getErrors reads, and clears, Maestro 2's error register, as s22
    does). maeGateMs is raised to 5000 ms inside nc_guard, so the fresh window outlasts the harness's round trip. The
    host must know NaviCore as its controller (?CONTROLLER,ON,20) to unicast the reply home."""
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        slot, dev, host = _hosted_remote_slot(nc, g.before)
        require_tokens(bench, host, f"?CONTROLLER,ON,{(g.before.get('wcbNetwork') or {}).get('deviceId') or 20}")
        nc.set_config({"maeGateMs": 5000})
        verb = f";M{dev},getErrors"
        action = {"type": "wcb_unicast", "target": str(host), "cmd": verb, "skipRunning": True}
        warm = f"[DISPATCH] Maestro {dev} via-WCB gate: cache stale → sent ;M{dev},getMovingState, fail-open"
        sent = f"[DISPATCH] WCB→{host}  {verb}"
        with nc.debug(DBG_MAESTRO | DBG_WCB):
            nm = nc.dev.mark()
            ack = nc.test_action(action)
            time.sleep(0.8)
            lines = _flushed(nc, nm, settle=0.3)
            mov = next((int(m.group(1)) for x in lines
                        for m in [re.match(rf'^\[MAE:{slot}\]\{{"q":"mov","val":(\d+)\}}', x)] if m), None)
            bench.note(f"slot {slot} (device {dev}, hosted by W{host}); the warm-up answered moving={mov}")
            if not ack.get("ok"):
                problems.append(f"stale cache: ACK {ack}")
            if warm not in lines:
                problems.append(f"stale cache: no {warm!r}")
            if sent not in lines:
                problems.append("stale cache: the verb was not sent (the gate must fail open)")
            if mov is None:
                problems.append(f"W{host} never answered the warm-up (no [MAE:{slot}] 'mov' marker)")
            elif mov == 0:
                nm = nc.dev.mark()
                nc.test_action(action)
                lines = _flushed(nc, nm, settle=0.5)
                if any("via-WCB gate: cache stale" in x for x in lines):
                    problems.append("fresh cache: NaviCore asked again although a 'not moving' reply was cached")
                if sent not in lines:
                    problems.append("fresh cache, not moving: the verb was not sent")
            else:
                bench.note(f"Maestro {dev} reported moving: the fresh-cache half is not checked")
    assert not problems, "; ".join(problems)


def _servo_channel(nc, cfg):
    """(slot, device, channel, position) of a local Maestro channel no passthrough knob drives, reading a servo
    position well inside 1000-2000 us (4400-7600 quarter-us), so a 100 us step stays in range; Skip otherwise."""
    for slot, dev in nc.local_slots(cfg):
        driven = {o.get("maestroCh") for k in (cfg.get("knobs") or {}).values() if k.get("function") == 1
                  for key in ("outputs", "outputs2", "outputs3") for o in (k.get(key) or []) if o.get("target") == slot}
        for ch in sorted(set(range(24)) - driven):
            pos = nc.mae_get(slot, ch)
            if isinstance(pos, int) and 4400 <= pos <= 7600:
                return slot, dev, ch, pos
            if not isinstance(pos, int):
                break
    raise Skip("no undriven local Maestro channel reads a servo position between 4400 and 7600")


@test("ncengine.maestro_skip_running_slot", "skipRunning on a Maestro action: on NaviCore's local Maestro a gated "
      "action is skipped while a slow move runs and goes once it has stopped; on a remote slot nobody answers for, the "
      "gate asks, fails open and the frame reaches W1 S1 (moves one dome servo 100 us, slowly, and back)",
      needs=["navicore", "wcb1"], links=["W1S1"])
def maestro_skip_running_slot(bench):
    """maestroSequenceBusy (NaviCore.ino:873-900): a LOCAL slot asks the Maestro (getMovingState on Serial2, cached for
    maeGateMs); a REMOTE slot reads the mesh cache and, when it is stale, broadcasts ';M<dev>,getMovingState'
    (maestroBroadcastReadVerb, :703-722) and fails open. A channel no passthrough knob drives gets speed 4 (100
    quarter-us/s, through TEST_ACTION with no gate) and a 400 quarter-us move, about a second: a gated setTarget sent at
    once must be skipped, and one sent after ?MAE,MOVING reads 0 must go (it moves the servo back). The action's own
    '[DISPATCH] Maestro <slot>  <cmd>' line prints before the gate (:2088-2089), so a skip shows as that line followed
    by 'skipped — already running'. Afterwards the channel's speed is 0 (the Maestro's 'no limit', which ?MAE,FREE and
    the config tool's timeline preview also leave): Pololu has no speed readback, so a Control Center speed limit on
    that channel, if it had one, is lost until the Maestro resets. The remote half uses a slot nobody hosts: nothing
    answers the warm-up and nothing moves."""
    l11 = link(bench, 1, "S1")
    nc = _nc(bench)
    cfg = nc.config()
    slot, _, ch, p0 = _servo_channel(nc, cfg)
    rslot, rdev = _remote_slot(cfg)
    p1 = p0 + 400 if p0 <= 6000 else p0 - 400
    problems = []

    def act(cmd, gated, target=slot):
        return {"type": "maestro", "target": str(target), "cmd": cmd, "skipRunning": gated}

    def settle(deadline_s=6.0):
        deadline = time.monotonic() + deadline_s
        while nc.mae_moving(slot) != 0 and time.monotonic() < deadline:
            time.sleep(0.2)
    with nc.debug(DBG_MAESTRO):
        try:
            nc.test_action(act(f"setSpeed,{ch},4", False))
            nc.test_action(act(f"setTarget,{ch},{p1}", False))
            nm = nc.dev.mark()
            nc.test_action(act(f"setTarget,{ch},{p1}", True))
            lines = _flushed(nc, nm, settle=0.2)
            if f"[DISPATCH] Maestro {slot} skipped — already running" not in lines:
                problems.append("a gated action during the slow move was not skipped")
            settle()
            reached = nc.mae_get(slot, ch)
            if reached != p1:
                problems.append(f"channel {ch} read {reached}, not {p1}, after the slow move")
            time.sleep(0.4)                           # past maeGateMs, so the gate asks the Maestro again
            nm = nc.dev.mark()
            nc.test_action(act(f"setTarget,{ch},{p0}", True))
            lines = _flushed(nc, nm, settle=0.2)
            if f"[DISPATCH] Maestro {slot}  setTarget,{ch},{p0}" not in lines or any("skipped" in x for x in lines):
                problems.append("a gated action after the move stopped was skipped or not dispatched")
            settle()
            m11, nm = l11.mark(), nc.dev.mark()
            nc.test_action(act("setTarget,5,6000", True, target=rslot))
            lines = _flushed(nc, nm, settle=0.3)
            if f"[DISPATCH] Maestro {rslot} gate: cache stale → sent getMovingState, fail-open" not in lines:
                problems.append(f"remote slot {rslot}: no 'cache stale ... fail-open' line")
            frame = _pololu(rdev, 0x84, 5, 6000)
            try:
                l11.expect(frame, timeout=3, since=m11)
            except AssertionError:
                problems.append(f"remote slot {rslot}: {frame.hex(' ')} never reached W1 S1")
        finally:
            nc.test_action(act(f"setTarget,{ch},{p0}", False))
            settle()
            nc.test_action(act(f"setSpeed,{ch},0", False))
    back = nc.mae_get(slot, ch)
    bench.note(f"slot {slot} ch {ch}: {p0} -> {p1} -> {p0} at speed 4, then speed 0; reads {back}")
    assert back == p0, f"channel {ch} reads {back}, not its starting {p0}"
    assert not problems, "; ".join(problems)


# ============================================================ periodic reports
@test("ncengine.mode_report_content", "modeReport sends its command to the configured WCB on a mode change and again "
      "60 s later: the template with {mode} filled, or that mode's own command when one is set (two mesh SET_MODEs: "
      "the mode-aware knobs re-dispatch)", needs=["navicore", "wcb1"], links=["W1S2"])
def mode_report_content(bench):
    """reportMode (rc_telemetry.h:1254-1263) sends rcModeReportCmd's string - cmds[mode-1] when set, else the template
    with '{mode}' replaced (rc_config.h:1977-1993) - to modeReport.wcb, from emitMode on every mode change and from
    tick() once 60 s have passed since the last report (rc_telemetry.h:1658-1660); every call re-stamps that 60 s.
    Inside nc_guard the report goes to W1 as ';S2...' markers: the template for every mode, and an override for the
    starting mode. A mesh SET_MODE (:2236-2243) makes the change and holds until the SBUS mode switch moves. The
    heartbeat is timed on the probe clock from the change's own report. W2 gets no ';V,MODE,' report meanwhile; the mode
    ends where it started, which is what W2 last heard."""
    l12 = link(bench, 1, "S2")
    w1 = usb_wcb(bench)
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        nid = (g.before.get("wcbNetwork") or {}).get("deviceId") or 20
        m0 = nc.mode()
        m1 = next(m for m in (2, 1, 3) if m != m0)
        tag = marker("M")
        cmds = ["", "", ""]
        cmds[m0 - 1] = f";S2{tag}O"
        nc.set_config({"modeReport": {"enabled": True, "wcb": 1, "template": f";S2{tag}{{mode}}", "cmds": cmds}})
        want1, want0 = f"{tag}{m1}", f"{tag}O"
        try:
            pm = l12.mark()
            w1.send(f';W{nid},{{"type":"SET_MODE","mode":{m1}}}')
            if want1.encode() + b"\r" not in _wait_all(l12, pm, [want1], 4.0):
                problems.append(f"no report {want1!r} after the change to mode {m1}")
            else:
                deadline = time.monotonic() + 66
                while _count(l12.received(pm), want1) < 2 and time.monotonic() < deadline:
                    time.sleep(0.5)
                t1, t2 = _probe_ms(l12, pm, want1 + "\r"), _probe_ms(l12, pm, want1 + "\r", nth=1)
                if t2 is None:
                    problems.append("no 60 s heartbeat report within 66 s of the change")
                else:
                    bench.note(f"mode {m1}: report, then the heartbeat {t2 - t1} ms later")
                    if not 59000 <= t2 - t1 <= 61500:
                        problems.append(f"the heartbeat came {t2 - t1} ms after the change's report, expected 60000")
            pm = l12.mark()
            w1.send(f';W{nid},{{"type":"SET_MODE","mode":{m0}}}')
            if want0.encode() + b"\r" not in _wait_all(l12, pm, [want0], 4.0):
                problems.append(f"no override report {want0!r} after the change back to mode {m0}")
            if f"{tag}{m0}".encode() in l12.received(pm):
                problems.append(f"mode {m0} sent the template although its own command is set")
        finally:
            if nc.mode() != m0:
                w1.send(f';W{nid},{{"type":"SET_MODE","mode":{m0}}}')
                time.sleep(1.5)
    assert not problems, "; ".join(problems)


STATS_FIELDS = ("sent", "ackd", "rty", "fail", "ung", "bcast", "recv")


def _reported(w1, nid):
    """W1's ?STATS row for what WCB<nid> reported about itself (WCB.ino:2174-2195) -> (dict, age in s), or (None, None)."""
    rx = re.compile(rf"^WCB{nid}: Sent: (\d+), ACKd: (\d+), Retries: (\d+), Failed: (\d+), Unguaranteed: (\d+), "
                    rf"Bcast: (\d+), Recv: (\d+)  \((\d+)s ago\)$")
    rows = [m for x in w1.run("?STATS", timeout=8) for m in [rx.match(x.rstrip())] if m]
    if not rows:
        return None, None
    v = [int(n) for n in rows[-1].groups()]
    return dict(zip(STATS_FIELDS, v[:7])), v[7]


@test("ncengine.stats_report_content", "statsReport: the row W1 lists for WCB 20 under 'Reported by Other Nodes' is "
      "NaviCore's own counters at the moment it sent them (between GET_MESH_STATS reads just before and just after), "
      "every 30 s; with wcb 0 nothing is sent", needs=["navicore", "wcb1"], links=[])
def stats_report_content(bench):
    """reportMeshStats (rc_telemetry.h:1291-1336) sends '?STATS,RPT,<id>,<sent>,<ackd>,<retries>,<failed>,
    <unguaranteed>,<bcast>,<recv>' to statsReport.wcb every 30 s, from the same sources GET_MESH_STATS's 'agg' reads
    (getAggregateStats, getBroadcastSent, g_meshRxCount; :1370-1384). The counters only rise, so a report sent between
    two GET_MESH_STATS reads lies between them field by field. W1 stores and lists it (storeReportedStats,
    WCB.ino:2210-2251). The wait starts ~3 s before the next report is due (W1 prints its age), and a report counts only
    when it replaces the row read after the first GET_MESH_STATS. Then, inside nc_guard, wcb 0 ('collect and display,
    ship nothing', rc_telemetry.h:1298) must leave W1's row ageing past 30 s. Skips unless the bench reports to W1."""
    w1 = usb_wcb(bench)
    nc = _nc(bench)
    cfg = nc.config()
    nid = (cfg.get("wcbNetwork") or {}).get("deviceId") or 20
    sr = cfg.get("statsReport") or {}
    if not sr.get("enabled") or sr.get("wcb") != 1:
        raise Skip(f"statsReport does not go to W1 ({sr})")
    problems = []
    row, age = _reported(w1, nid)
    if row is None:
        time.sleep(32)
        row, age = _reported(w1, nid)
        if row is None:
            raise AssertionError(f"W1 lists no report from WCB {nid} after 32 s")
    if 30 - age - 3 > 0:
        time.sleep(30 - age - 3)
    before = nc.mesh_stats()["agg"]
    row0, _ = _reported(w1, nid)
    new, deadline = None, time.monotonic() + 36
    while time.monotonic() < deadline:
        row2, _ = _reported(w1, nid)
        if row2 is not None and row2 != row0:
            new = row2
            break
        time.sleep(0.7)
    after = nc.mesh_stats()["agg"]
    if new is None:
        problems.append(f"no new report from WCB {nid} reached W1 within 36 s")
    else:
        bench.note(f"GET_MESH_STATS before {before}; reported {new}; after {after}")
        for k in STATS_FIELDS:
            if not before.get(k, -1) <= new[k] <= after.get(k, -1):
                problems.append(f"{k}: reported {new[k]}, not between {before.get(k)} and {after.get(k)}")
    with nc_guard(bench) as g:
        g.nc.set_config({"statsReport": {"enabled": True, "wcb": 0}})
        _, a0 = _reported(w1, nid)
        time.sleep(35)
        _, a1 = _reported(w1, nid)
        bench.note(f"with statsReport.wcb 0: W1's row aged {a0} s -> {a1} s over 35 s")
        if a0 is None or a1 is None or a1 < a0 + 30:
            problems.append(f"with statsReport.wcb 0 W1's row did not age past 30 s ({a0} s, then {a1} s)")
    assert not problems, "; ".join(problems)
