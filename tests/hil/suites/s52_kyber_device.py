"""The real Kyber on WCB3 (docs/HIL_TESTING.md "WCB3, probe 4 and the Kyber"): a pad button pressed through the SBUS
controller's output B, and what the Kyber sends for it followed end to end - out its MarcDuino port into W3 S5, a
command line WCB3 runs, and out its Maestro port into W3 S2, which WCB3 (Kyber local) bridges to every Maestro_Remote
board's Maestro port. These are the proofs of the three bench wires the s22 kyber.* tests, which stand a probe in for
the Kyber on W1, never touch: bench.json device_links sbus:S4 -> kyber, kyber:MARCDUINO -> W3S5, kyber:MAESTRO -> W3S2.

How a button is pressed. The Kyber reads its button pad on one SBUS channel (`_buttonsCH`, CH7 - the channel
NaviCore's matrix reads too) and takes a button when the channel's value is within ±50 of that button's (`B<n>PWM`;
`B0PWM` is Released, 991 on this bench). Its values are raw SBUS: the Kyber's host link (`RC` on its own USB, framed
`@K{...}*<sum>`) read CH7 as exactly the raw value the controller held (2026-10-06). The controller holds a raw value on
a channel (SbusCtl.channel, the INF8 "ch" verb), and route("kyber") puts its controls on output B alone: output A
keeps NaviCore on the rest frame it took at boot, so NaviCore's matrix sees none of it (sbus.route_isolates). The
channel goes to Released before the route, so the Kyber never starts on a button, and every press is a hold of
PRESS_S, then Released: the Kyber smooths each channel (`filter7` 0.08, an exponential average), so a short hold never
settles inside a window - 0.4 s holds were missed whenever the value was more than ~20 from a button's. A button within
RELEASED_GAP of Released cannot be told from it (the standard ladder's button 8, 988, beside Released 991) and is
left out.

Which config. The Kyber runs the table its own `GET` returns, saved as kyber/bench_kyber_live.json. Until 2026-10-06
that was the standard KyberPad ladder (274..1702 in steps of 102), not Greg's kyber/bench_kyber_config.json (pasted
2026-10-05: 176..788); that day Greg's config was loaded onto it over the host link (`SETM` of the 35 keys that
differed, then `COMMIT`; a reboot showed every key kept) and the live file read again, so the two agree. Both have
Released 991. kyber_config() reads the live file when it is there: it is what the device runs, whatever was pasted.

What else the Kyber does while it hears a radio: RC channels 1-3 pass through to Maestro channels (`Channel1-3` /
`MChannel1-3`), so it streams setTargets at their rest (992: centre) the whole time - W2's real Maestro 2 may centre a
servo (listed in hil/servos.py). Routed back to NaviCore it hears nothing and goes quiet, as a dormant Kyber does (Greg,
2026-10-05). The route is RAM only and every test puts it back (SbusCtl.clear_faults), with the pad channel as found.

Messages quote the commands and frames this file chose from the Kyber's own config, and byte counts.
"""
import json
import os
import re
import time

from hil.runner import Skip, test
from hil.sbus import SbusCtl
from hil.wcb import WCB
from suites.common import link

KYBER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kyber")
KYBER_LIVE = os.path.join(KYBER_DIR, "bench_kyber_live.json")     # the device's own GET (what it runs)
KYBER_CFG = os.path.join(KYBER_DIR, "bench_kyber_config.json")    # Greg's pasted config (another ladder)
SETTLE_S = 1.5        # routed to the Kyber: its SBUS input locks and its pad reads Released before the first press
PRESS_S = 1.2         # a press: the button's value held this long - the 0.08 channel filter settles in well under it
RELEASE_S = 1.5       # back at Released before the next press, for the filter to leave the last button's window
RELEASED_GAP = 100    # a button closer than this to Released overlaps its ±50 window: it cannot be pressed apart
REPLY_S = 3.0         # a MarcDuino command through W3 and the mesh to a probe-wired port
MAESTRO_S = 2.0       # the Kyber's Maestro frames bridged out of W3 to W1 S1
# A WCB-command MarcDuino string: ;w<board>;s<port><text>, the form a WCB runs as "send <text> out W<board> S<port>".
WCB_PORT_CMD = re.compile(r"^;[wW](\d+);[sS]([1-5])(.+)$")


# ------------------------------------------------------------------ the Kyber's own config
def kyber_config():
    """The Kyber's config -> its dict: bench_kyber_live.json (what the device runs) when it is there, else
    bench_kyber_config.json; Skip when neither reads."""
    for p in (KYBER_LIVE, KYBER_CFG):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    raise Skip(f"no readable Kyber config in {KYBER_DIR}")


def pad_ladder(cfg):
    """(the pad's SBUS channel, its Released value, {button: value}) from the Kyber's config: `_buttonsCH`, `B0PWM` and
    `B<n>PWM` for every button with a non-zero value."""
    ch = int(cfg.get("_buttonsCH") or 0)
    released = int(cfg.get("B0PWM") or 0)
    values = {}
    for k, v in cfg.items():
        m = re.fullmatch(r"B(\d+)PWM", k)
        if m and int(m.group(1)) > 0 and int(v or 0) > 0:
            values[int(m.group(1))] = int(v)
    return ch, released, values


def marcduino_cmds(cfg):
    """{button: the MarcDuino line it sends} - `MDF<n>`, whose escapes (\\r) the Kyber expands as it sends - for every
    button with one."""
    out = {}
    for k, v in cfg.items():
        m = re.fullmatch(r"MDF(\d+)", k)
        if m and isinstance(v, str) and v.strip():
            out[int(m.group(1))] = v.replace("\\r", "\r").replace("\\n", "\n")
    return out


def port_cmd_cases(cfg):
    """[(button, its value, the command, board, port, the text that must come out that port)] for every pad button
    whose MarcDuino line is a WCB port command (;w<n>;s<p><text>, CR-ended), in button order."""
    ch, released, values = pad_ladder(cfg)
    cases = []
    for n, line in sorted(marcduino_cmds(cfg).items()):
        m = WCB_PORT_CMD.match(line.rstrip("\r\n"))
        if m and n in values and abs(values[n] - released) >= RELEASED_GAP:
            cases.append((n, values[n], line.rstrip("\r\n"), int(m.group(1)), f"S{m.group(2)}", m.group(3)))
    return cases


def maestro_script_buttons(cfg):
    """{button: (maestro, script)} for every pad button that runs a Maestro script on its press (`B<n>M<m>Script`, 1 or
    2), in button order."""
    out = {}
    for k, v in cfg.items():
        m = re.fullmatch(r"B(\d+)M([12])Script", k)
        if m and int(v or 0) > 0 and int(m.group(1)) > 0:
            out.setdefault(int(m.group(1)), (int(m.group(2)), int(v)))
    return dict(sorted(out.items()))


def script_frames(data, device):
    """Every Pololu-protocol restartScript frame to `device` in `data` - AA <device> 27 <sub> (0xA7 with its MSB
    cleared) and the parameter form AA <device> 28 <sub> <lo> <hi> - as hex strings, in order."""
    out = []
    for m in re.finditer(rb"\xAA" + bytes([device & 0x7F]) + rb"([\x27\x28])", data):
        n = 4 if m.group(1) == b"\x27" else 6
        out.append(data[m.start():m.start() + n].hex(" "))
    return out


# ------------------------------------------------------------------ the bench
def _ctl(bench):
    """The SBUS controller with its INF8 verbs probed and a second output, or Skip. A test state an earlier test left
    changed is put back first, and noted."""
    ctl = SbusCtl(bench.dev("sbus"))
    st = ctl.test_state()
    if st is None:
        raise Skip("the SBUS controller has no INF8 test verbs: flash SBUSController's kyber-sbus image, app only")
    if not ctl.has_route():
        raise Skip("the SBUS controller has no second SBUS output: flash SBUSController's kyber-sbus image, app only")
    left = ctl.clear_faults()
    if left:
        bench.note(f"the controller's test state was left changed; put back: {left}")
        time.sleep(1.0)
    return ctl


def _wcb3_kyber_local(bench):
    """WCB3 (its own USB) with the Kyber local on it, or Skip: ?KYBER,LIST must say 'Kyber is Local'. -> (WCB, the
    targeting line)."""
    if "W3S2" not in (bench.cfg.get("port_devices") or {}):
        raise Skip("bench.json port_devices has no Kyber on W3 S2: the real Kyber is not wired")
    w3 = WCB(bench.dev("wcb3"))
    lines = w3.run("?KYBER,LIST")
    if not any(x.startswith("Kyber is Local") for x in lines):
        raise Skip(f"WCB3 is not Kyber local ({[x for x in lines if x.startswith('Kyber')] or lines[:3]}): "
                   f"?KYBER,LOCAL,S2 and a reboot set it up")
    return w3, next((x for x in lines if x.startswith("Targeting mode:")), "")


class KyberPad:
    """The Kyber's button pad driven through the controller, routed to output B for the block and put back after:
    the route to NaviCore (clear_faults), and the pad channel at the value NaviCore read there at the start (`rest`)."""

    def __init__(self, bench, ctl, ch, released, rest):
        self.bench, self.ctl, self.ch, self.released, self.rest = bench, ctl, ch, released, rest

    def __enter__(self):
        self.ctl.channel(self.ch, self.released)
        st = self.ctl.route("kyber")
        if st.get("route") != "kyber":
            raise AssertionError(f"the controller did not take the route to the Kyber: {st}")
        time.sleep(SETTLE_S)
        return self

    def press(self, value):
        self.ctl.channel(self.ch, value)
        time.sleep(PRESS_S)
        self.ctl.channel(self.ch, self.released)
        time.sleep(RELEASE_S)

    def __exit__(self, *exc):
        problems = []
        try:
            self.ctl.channel(self.ch, self.released)
            done = self.ctl.clear_faults()
            if not any("routed back" in x for x in done):
                problems.append(f"the route back to NaviCore was not confirmed: {done}")
            self.ctl.channel(self.ch, self.rest)
        except AssertionError as e:
            problems.append(f"the controller could not be put back: {e}")
        if problems:
            self.bench.note("; ".join(problems))
            if exc[0] is None:
                raise AssertionError("; ".join(problems))
        return False


def _pad_rest(bench, ch):
    """The value NaviCore reads on the pad channel now (#L09, through output A), to put back after the test; the
    Kyber's Released value when NaviCore is not there to ask."""
    try:
        from hil.navicore import NaviCore
        chans = NaviCore(bench.dev("navicore")).sbus_dump()["channels"]
        return chans[ch - 1] if len(chans) >= ch else None
    except Exception:  # noqa: BLE001 - no NaviCore here: the controller's rest is 992 on every channel
        return None


# ------------------------------------------------------------------ the tests
@test("kyber.device_pad_serial", "The real Kyber's pad buttons, pressed through the SBUS controller's output B, send "
      "their MarcDuino commands into W3 S5 and WCB3 runs them: each button whose command is a WCB port command (on this "
      "bench 3 ';w3;s3track1', 7 ';w2;s4:PP100' and 8 ';w2;s4:PH') puts exactly its text and a CR out that port, once, "
      "and nothing comes with the pad released", needs=["sbus", "wcb3", "wcb2"], links=["W2S4|W3S3"])
def device_pad_serial(bench):
    """The proof of two bench wires at once: the controller's output B into the Kyber (it read the pad) and the Kyber's
    MarcDuino port into W3 S5 (WCB3 read its line). A WCB runs a line that starts with its command character as
    commands; ';w2;s4:PP100' sends ';s4:PP100' to WCB2, which writes ':PP100' and a CR out its S4, and ';w3;s3track1'
    is WCB3's own S3 - both probe-wired ports on this bench. The case list comes from the Kyber's own config (port_cmd_cases); one whose port has no probe wire is
    left out and noted. First a quiet window with the pad Released: the Kyber sends a MarcDuino line only on a press,
    so the port must stay silent."""
    cfg = kyber_config()
    ch, released, _ = pad_ladder(cfg)
    if not ch or not released:
        raise Skip("the Kyber config names no button pad channel (_buttonsCH) or Released value (B0PWM)")
    cases, skipped = [], []
    for case in port_cmd_cases(cfg):
        n, value, cmd, board, port, text = case
        (cases if bench.links.get(board, port) else skipped).append(case)
    if skipped:
        bench.note("left out, no probe wire on their port: " + "; ".join(f"button {n} {c!r} (W{b}{p})"
                                                                        for n, _, c, b, p, _ in skipped))
    if not cases:
        raise Skip("no Kyber pad button sends a WCB port command to a probe-wired port")
    _wcb3_kyber_local(bench)
    ctl = _ctl(bench)
    wires = {}
    for _, _, _, board, port, _ in cases:
        if (board, port) not in wires:
            wires[(board, port)] = link(bench, board, port)
            wires[(board, port)].listen()
    problems, seen = [], []
    try:
        with KyberPad(bench, ctl, ch, released, _pad_rest(bench, ch) or released) as pad:
            marks = {k: l.mark() for k, l in wires.items()}
            time.sleep(1.0)
            quiet = {f"W{b}{p}": l.received(marks[(b, p)]) for (b, p), l in wires.items()}
            quiet = {k: v for k, v in quiet.items() if v}
            if quiet:
                problems.append(f"with the pad Released, bytes came out {quiet}")
            for n, value, cmd, board, port, text in cases:
                l = wires[(board, port)]
                m = l.mark()
                pad.press(value)
                want = text.encode() + b"\r"
                deadline = time.monotonic() + REPLY_S
                while time.monotonic() < deadline and want not in l.received(m):
                    time.sleep(0.05)
                time.sleep(0.3)
                got = l.received(m)
                seen.append(f"button {n} (CH{ch} {value}) {cmd!r}: W{board}{port} got {got!r}")
                if got != want:
                    problems.append(f"button {n} (CH{ch} {value}, {cmd!r}): W{board}{port} got {got!r}, expected "
                                    f"{want!r} once" + (" - nothing: did the Kyber hear the press (SBUS B), and WCB3 its "
                                                        "line (W3 S5 at 9600)?" if not got else ""))
    finally:
        for l in wires.values():
            try:
                l.release()
            except Exception:  # noqa: BLE001 - the runner releases it too
                pass
    bench.note("kyber.device_pad_serial: " + "; ".join(seen))
    assert not problems, "; ".join(problems)


@test("kyber.device_pad_maestro", "The real Kyber's Maestro buttons, pressed through the SBUS controller's output B, "
      "make it send a restartScript frame on its Maestro port into W3 S2, which WCB3 (Kyber local) bridges over the mesh "
      "to Maestro 1's port: W1 S1 (the probe that stands in for Maestro 1) gets AA 01 27 <sub> for button 1 "
      "(Maestro 1, script 1)", needs=["sbus", "wcb3", "wcb1"], links=["W1S1"])
def device_pad_maestro(bench):
    """The proof of the Kyber's Maestro port into W3 S2 (and again of output B into the Kyber). A Kyber button with a
    Maestro 1 script (`B<n>M1Script`) sends Maestro 1 a Pololu restartScript; WCB3, Kyber local, forwards every byte
    its Kyber port reads to the mesh (broadcast, or targeted at the board hosting that Maestro), and W1, Maestro_Remote,
    writes them to its S1, where probe 1 stands in for Maestro 1. While it hears the radio the Kyber also streams the
    pass-through channels' setTargets, so the frames are looked for among them (script_frames). W2's real Maestro 2 sees
    the same broadcast: it ignores device 1's frames, but the pass-through ones may centre its servos.

    The mesh hop is an unacknowledged broadcast, so W1 can miss it (button 1 reached nothing in run 20261010-004124, its
    last test, after passing in probe.device_links at the start). As s44's Wires does, a button whose frame W1 S1 did
    not get is judged by the W2 S1 tap, the other Maestro_Remote board's copy of the same broadcast: there, W1 missed
    it (noted); nowhere, the button is pressed once more (noted) before it fails."""
    cfg = kyber_config()
    ch, released, values = pad_ladder(cfg)
    buttons = {n: ms for n, ms in maestro_script_buttons(cfg).items() if ms[0] == 1 and n in values}
    if not ch or not released or not buttons:
        raise Skip("the Kyber config has no pad button that runs a Maestro 1 script")
    _, targeting = _wcb3_kyber_local(bench)
    ctl = _ctl(bench)
    s1 = link(bench, 1, "S1")
    s1.listen()
    w2 = bench.links.get(2, "S1")             # the other Maestro_Remote board's copy of the broadcast, if wired
    if w2 is not None:
        w2.listen()
    problems, seen, lost = [], [], []
    try:
        with KyberPad(bench, ctl, ch, released, _pad_rest(bench, ch) or released) as pad:
            m0 = s1.mark()
            time.sleep(1.0)
            idle = s1.received(m0)
            for n, (maestro, script) in buttons.items():
                for attempt in (1, 2):
                    m, mw = s1.mark(), (w2.mark() if w2 is not None else None)
                    pad.press(values[n])
                    time.sleep(MAESTRO_S)
                    got = s1.received(m)
                    frames = script_frames(got, maestro)
                    if frames:
                        break
                    if w2 is not None and script_frames(w2.received(mw), maestro):
                        lost.append(f"button {n}: W1 S1 missed the broadcast {w2.key} got")
                        frames = script_frames(w2.received(mw), maestro)
                        break
                    if attempt == 1:
                        lost.append(f"button {n}: no board got its frame; pressed once more")
                seen.append(f"button {n} (CH{ch} {values[n]}, Maestro {maestro} script {script}): {len(got)} bytes on "
                            f"W1 S1, restartScript frames {frames or 'none'}")
                if not frames:
                    problems.append(f"button {n} (Maestro {maestro} script {script}): no restartScript frame to device "
                                    f"{maestro} reached W1 S1 in {MAESTRO_S:g} s ({len(got)} other bytes"
                                    + (")" if got else "; nothing at all: did the Kyber hear the radio, and does WCB3 "
                                       "bridge its Maestro port?)"))
            if script_frames(idle, 1):
                problems.append(f"with the pad Released a restartScript frame reached W1 S1: {script_frames(idle, 1)}")
    finally:
        for l in (s1, w2):
            try:
                if l is not None:
                    l.release()
            except Exception:  # noqa: BLE001 - the runner releases it too
                pass
    if lost:
        bench.note("unacknowledged Kyber broadcast: " + "; ".join(lost))
    bench.note(f"kyber.device_pad_maestro ({targeting or 'targeting mode not printed'}): " + "; ".join(seen)
               + f"; {len(idle)} bytes on W1 S1 in the 1 s before the first press (the pass-through setTargets)")
    assert not problems, "; ".join(problems)
