"""The SBUS controller (SBUSController on a WCB HW 3.2, the bench's `sbus` device) and an SBUS frame codec.
docs/hil_plan/NAVICORE.md §3 INF2.

The controller reads one JSON object per line on USB (processCommandJson, SBUSController.ino:1036-1360). Every line sent
here starts with '{': outside a JSON line single characters are commands, and 'm' and 'w' change and save its settings
(handleSerialCommands :1886-1900; docs/HIL_TESTING.md §1). Of the JSON verbs, 'mode', 'cfg' and 'wificfg' save too
(:1213-1225, :1238, :1352), so SbusCtl has no method for them. What it does send changes RAM only, with no timeout, and
lasts until the controller resets: a test puts back what it moved (center_all() for sticks, buttons and button-mode
trims). It transmits SBUS-24 every 9 ms with a constant flags byte 0x00 (:110-129, :944-955), and NaviCore decodes it
(NaviCore sbus_reader.h); every SBUS channel it moves, NaviCore re-emits on SBUS OUT (hil/servos.py).

The INF8 test verbs (NAVICORE.md INF8, D-NC8; SBUSController's local branch hil-week, not its main) add what a real
receiver sends and this transmitter never does: the flags byte, a stopped stream, the SBUS-16 frame for one boot, a
malformed burst, a raw channel value. All RAM only, cleared by any reset. Each answers the asker with {"e":"test",...}
and the test state; an older image stays silent, which test_state() reports as None. The "mode" verb is the one hazard:
without "save":false it saves, and an older image ignores "save", so SbusCtl sends it only after a probe answered.
ReaderModel is NaviCore's framing byte for byte, so a test knows what each burst should decode to.
"""
import json
import re
import time

from .checkpoint import redact_text
from .navicore import entries
from .serialdev import ExpectTimeout, usb_jtag_reset

SBUS_MIN, SBUS_CENTER, SBUS_MAX = 172, 992, 1811          # SBUSController.ino:113-115
HEADER, FOOTER = 0x0F, 0x00                               # :110-111; NaviCore sbus_reader.h checks both
FRAME_LEN = {16: 25, 24: 36}                              # 0x0F + 22 or 33 data bytes + flags + 0x00 (:122-125)
# The flags byte, just before the footer: bits 0-1 are the digital channels 17/18 of the 16-channel standard, and
# NaviCore reads 0x04 as a lost frame and 0x08 as failsafe (sbus_reader.h decodeFrame). The controller always sends 0.
FLAG_CH17, FLAG_CH18, FLAG_LOST, FLAG_FAILSAFE = 0x01, 0x02, 0x04, 0x08

# The INF8 test verbs (SBUSController.ino, hil-week: processCommandJson "flags", "stream", "ch", "glitch", "mode" with
# "save":false; sendTestReply; sendGlitch). Every reply starts {"e":"test","t":"<verb>".
TEST_REPLY = r'^\{"e":"test","t":"%s"'
GLITCHES = ("truncate", "garbage", "double", "gap", "dip")    # sendGlitch's kinds; the controller checks each n
# The "route" verb (SBUSController branch kyber-sbus): the controls on output A (S5, NaviCore), B (S4, a Kyber) or both.
ROUTES = ("navicore", "kyber", "both")


# ------------------------------------------------------------------ the frame codec
def encode(channels, flags=0, n=24):
    """`n` channel values (0-2047) and a flags byte -> one SBUS frame: 25 bytes for n=16, 36 for the SBUS-24 variant
    (n=24). Packed exactly as the controller's buildSbusFrame (SBUSController.ino:757-772): 0x0F, the channels as
    consecutive 11-bit values from byte 1, least significant bit first, the flags byte, 0x00. The controller clamps a
    value into 0-2047; here one outside it is a ValueError, since an expected frame built from it would be a test bug."""
    if n not in FRAME_LEN:
        raise ValueError(f"n must be 16 or 24, not {n}")
    if len(channels) != n:
        raise ValueError(f"{len(channels)} channel values for an SBUS-{n} frame")
    if not 0 <= flags <= 0xFF:
        raise ValueError(f"flags {flags} is not one byte")
    frame = bytearray(FRAME_LEN[n])
    frame[0] = HEADER
    for i, v in enumerate(channels):
        if not 0 <= v <= 0x7FF:
            raise ValueError(f"channel {i + 1} = {v}: SBUS carries 11 bits (0-2047)")
        b = i * 11
        frame[1 + b // 8] |= (v << (b % 8)) & 0xFF
        frame[2 + b // 8] |= (v >> (8 - b % 8)) & 0xFF
        if b % 8 > 5:
            frame[3 + b // 8] |= (v >> (16 - b % 8)) & 0xFF
    frame[-2], frame[-1] = flags, FOOTER
    return bytes(frame)


def decode(data):
    """One SBUS frame -> {'n': 16 or 24, 'channels': [n ints], 'flags': int, 'lost': bool, 'failsafe': bool}. The length
    picks the variant, as NaviCore's reader decides it: 25 or 36 bytes, 0x0F first, 0x00 last (sbus_reader.h
    tryParseAndReset, decodeFrame). Anything else is an AssertionError: these are bytes a device sent."""
    data = bytes(data)
    n = {25: 16, 36: 24}.get(len(data))
    if n is None:
        raise AssertionError(f"not an SBUS frame: {len(data)} bytes (25 for SBUS-16, 36 for SBUS-24)")
    if data[0] != HEADER or data[-1] != FOOTER:
        raise AssertionError(f"not an SBUS frame: header {data[0]:02X}, footer {data[-1]:02X} (0F and 00)")
    channels = []
    for i in range(n):
        b = i * 11
        v = data[1 + b // 8] >> (b % 8)
        v |= data[2 + b // 8] << (8 - b % 8)
        if b % 8 > 5:
            v |= data[3 + b // 8] << (16 - b % 8)
        channels.append(v & 0x7FF)
    flags = data[-2]
    return {"n": n, "channels": channels, "flags": flags, "lost": bool(flags & FLAG_LOST),
            "failsafe": bool(flags & FLAG_FAILSAFE)}


# ------------------------------------------------------------------ NaviCore's framing, modelled
READER_LOCK_FRAMES = 3                 # sbus_reader.h LOCK_FRAMES (:221): frames of one variant in a row before a decode
READER_OVERFLOW = FRAME_LEN[24] + 4    # :177, the buffer-overflow guard: the buffer is dropped at this many bytes


class ReaderModel:
    """NaviCore's SBUS framing byte for byte (NaviCore sbus_reader.h SbusReader::read :76-192, tryParseAndReset
    :232-265), with the timing reduced to what a test controls: bursts of back-to-back bytes, and silences longer than
    INTER_FRAME_GAP_US (1.5 ms) between them, during which NaviCore's loop() calls read() again. Structure only:
    `decoded` lists the frames the reader decodes, as the bytes #L13 would show, in order. Left out: a loop() stall in
    the middle of a burst, which makes the reader see a gap there (it reads micros() once per call), and the in-loop
    gap flush of a complete buffer, which the closing flush in a silence has always taken first. locked=16 or 24 starts
    it locked on that variant and idle, as NaviCore is after a stream at full rate."""

    def __init__(self, locked=None):
        self.buf, self.in_frame, self.pending16 = bytearray(), False, False
        self.streak, self.variant, self.frame_len = 0, 0, 0      # lockStreak_, lockVariant_, detectedFrameLen
        self.decoded = []
        if locked:
            self.streak, self.variant, self.frame_len = READER_LOCK_FRAMES, locked, FRAME_LEN[locked]

    def _complete(self):
        """bufIsCompleteFrame: exactly 25 or 36 bytes, the header first and the footer last."""
        return len(self.buf) in (FRAME_LEN[16], FRAME_LEN[24]) and self.buf[0] == HEADER and self.buf[-1] == FOOTER

    def _parse(self):
        """tryParseAndReset: a structurally valid frame adds to its variant's streak (another variant starts a new one)
        and decodes once the streak reaches READER_LOCK_FRAMES; anything else breaks it. The buffer empties either way."""
        variant = {FRAME_LEN[16]: 16, FRAME_LEN[24]: 24}[len(self.buf)] if self._complete() else 0
        if not variant:
            self.streak, self.variant = 0, 0
        else:
            if variant == self.variant:
                self.streak = min(self.streak + 1, READER_LOCK_FRAMES)
            else:
                self.variant, self.streak = variant, 1
            if self.streak >= READER_LOCK_FRAMES:
                self.decoded.append(bytes(self.buf))
                self.frame_len = len(self.buf)
        self.buf, self.in_frame = bytearray(), False

    def burst(self, data):
        """Bytes that arrive back to back -> self. Out of a frame only a header starts one (after an eager 25-byte
        decode, a first byte that is not a header drops the lock: :134-141); in a frame every byte is buffered, a locked
        reader decodes the moment its frame length ends on a footer (:167-174), and 40 bytes drop the buffer (:177)."""
        for b in bytes(data):
            if not self.in_frame:
                if self.pending16:
                    self.pending16 = False
                    if b != HEADER:
                        self.streak = self.variant = self.frame_len = 0
                if b == HEADER:
                    self.buf, self.in_frame = bytearray([b]), True
                continue
            self.buf.append(b)
            if (self.streak >= READER_LOCK_FRAMES and self.frame_len and len(self.buf) == self.frame_len
                    and b == FOOTER):
                self.pending16 = self.frame_len == FRAME_LEN[16]
                self._parse()
                continue
            if len(self.buf) >= READER_OVERFLOW:
                self.buf, self.in_frame = bytearray(), False
        return self

    def silence(self):
        """A pause past INTER_FRAME_GAP_US -> self: read()'s closing flush (:187-189) parses a complete buffer of either
        length. A partial one stays, and the next burst's bytes go on filling it."""
        if self.in_frame and self._complete():
            self._parse()
        return self

    @property
    def idle(self):
        """No partial frame buffered."""
        return not self.in_frame


def glitch_bursts(kind, n, frame, hex_=None):
    """What the controller puts on the wire for {"t":"glitch","kind":kind,"n":n} in place of `frame`, the frame it would
    have sent (SBUSController.ino sendGlitch) -> [burst], bytes each; a silence follows every burst. 'garbage' needs the
    bytes its reply echoed (`hex_`)."""
    frame = bytes(frame)
    size = len(frame)
    if kind == "truncate":
        return [frame[:min(n, size - 1)]]
    if kind == "garbage":
        if hex_ is None:
            raise ValueError("a garbage glitch's bytes come from its reply's hex")
        return [bytes.fromhex(hex_)]
    if kind == "double":
        return [frame * n]
    if kind == "gap":
        return [frame[:size // 2], frame[size // 2:]]
    if kind == "dip":
        d = decode(frame)
        channels = list(d["channels"])
        channels[n - 1] = SBUS_CENTER
        return [encode(channels, d["flags"], d["n"])]
    raise ValueError(f"unknown glitch {kind!r} (one of {', '.join(GLITCHES)})")


def reader_decodes(frame, bursts=(), trailing=0, locked=None):
    """What NaviCore's framing (ReaderModel) decodes from `bursts`, each followed by a silence, then `trailing` copies of
    the whole `frame`, each followed by a silence. It starts idle and locked on `frame`'s variant unless `locked` names
    one -> (the frames it decodes, the model afterwards)."""
    m = ReaderModel(locked or {FRAME_LEN[16]: 16, FRAME_LEN[24]: 24}[len(frame)])
    for b in bursts:
        m.burst(b).silence()
    for _ in range(trailing):
        m.burst(frame).silence()
    return m.decoded, m


# ------------------------------------------------------------------ the controller against NaviCore's bindings
def channel_of(entry):
    """The SBUS channel a GET_CONFIG switch or knob entry names (its inner key names were not re-read, so read
    leniently)."""
    return entry.get("channel", entry.get("c", entry.get("ch"))) if isinstance(entry, dict) else None


def safe_channels(ncfg):
    """SBUS channels NaviCore (GET_CONFIG `ncfg`) binds to nothing. Conservative: every channel a switch or knob names
    is unsafe, as is the matrix channel."""
    unsafe = {ncfg.get("matrixChannel")}
    unsafe |= {channel_of(e) for e in entries(ncfg.get("switches")) + entries(ncfg.get("knobs"))}
    unsafe.discard(None)
    return set(range(1, 25)) - unsafe


def band(ncfg, value):
    """The matrix slot whose threshold band holds `value` (first match; a 0/0 band is inert; NaviCore.ino:568-579)."""
    for i, t in enumerate(ncfg.get("thresholds", [])):
        lo, hi = (t.get("minPwm", t.get("min")), t.get("maxPwm", t.get("max"))) if isinstance(t, dict) else (t[0], t[1])
        if (lo, hi) != (0, 0) and lo is not None and hi is not None and lo <= value <= hi:
            return i + 1
    return None


def matrix_button(nc, cfg, ncfg, mode):
    """(index, button, slot) of a controller button (getcfg `cfg`) on NaviCore's matrix channel that decodes to a slot
    with no mapping in `mode`, where both the channel's resting value and the release value 992 decode to no slot.
    Pressing it emits rc_trig and runs nothing. Skip when there is none. `nc` is a hil.navicore.NaviCore."""
    from .runner import Skip
    mc = ncfg.get("matrixChannel")
    rest = nc.sbus_dump()["channels"][mc - 1]
    if band(ncfg, rest) or band(ncfg, SBUS_CENTER):
        raise Skip("the matrix channel's resting or release value already decodes to a slot")
    for i, b in enumerate(cfg.get("btn", [])):
        slot = band(ncfg, b.get("v", 0)) if b.get("c") == mc else None
        if slot and str(mode * 100 + slot) not in ncfg.get("mappings", {}):
            return i, b, slot
    raise Skip("no controller button on the matrix channel decodes to an unmapped slot")


class SbusCtl:
    """The SBUS controller on its USB port (a SerialDevice)."""

    # A reply can sit in the controller's USB outbox until the host sends another byte: HWCDC latches `connected`
    # false after 100 ms in which the host did not drain, the pump then holds everything queued, and only the RX path
    # re-arms it (SBUSController.ino serialOutboxPump). The controller's own UI pings every 3 s, which is what lets it
    # through there. Run 20260922-095341: a getcfg reply came 5.2 s late, 1 ms ahead of the next test's pong; runs
    # 20260915-085309 and 20260928-005805 failed the same way. So a wait for a reply pings every NUDGE_S.
    NUDGE_S = 1.0

    def __init__(self, dev):
        self.dev = dev
        self.verbs = None       # the INF8 test state from the last reply; None until a probe answered (test_state)

    def send(self, obj):
        """One JSON verb, compact, on one line. Never anything but a dict: see the module docstring."""
        self.dev.send(json.dumps(obj, separators=(",", ":")))

    # ------------------------------------------------------------ replies
    def _reply(self, pattern, since, timeout):
        """The first line matching `pattern` at or after mark `since`, pinging every NUDGE_S until it comes (see
        NUDGE_S). A pong is queued behind the reply in the same outbox, so it can never split the line."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            try:
                return self.dev.expect(pattern, timeout=max(0.0, min(self.NUDGE_S, left)), since=since)
            except ExpectTimeout as e:
                if left <= self.NUDGE_S:
                    raise ExpectTimeout(re.sub(r"within [0-9.e-]+s", f"within {timeout:g}s, pinging every "
                                               f"{self.NUDGE_S:g}s", str(e), count=1)) from None
            self.send({"t": "ping"})

    def ping_once(self, timeout=3.0):
        """One {"t":"ping"} -> the firmware version from its pong ({"t":"pong","ver":N,"fwver":"..."},
        SBUSController.ino:1048-1071); ExpectTimeout when none comes. A ping also opens the 5 s window in which the
        controller mirrors its broadcasts, getcfg's reply among them, to USB (:1000-1011)."""
        m = self.dev.mark()
        self.send({"t": "ping"})
        return self.dev.expect(r'^\{"t":"pong".*"fwver":"([^"]+)"', timeout=timeout, since=m).group(1)

    def ping(self, timeout=60.0):
        """Ping until it answers -> the firmware version. Opening the port can reset the controller, and its WiFi
        cascade can hold its boot about 50 s (docs/HIL_TESTING.md §2), so each try waits 3 s, up to `timeout`. A send
        that fails is not retried."""
        deadline = time.monotonic() + timeout
        while True:
            m = self.dev.mark()
            self.send({"t": "ping"})
            try:
                return self.dev.expect(r'^\{"t":"pong".*"fwver":"([^"]+)"', timeout=3, since=m).group(1)
            except AssertionError:
                if time.monotonic() > deadline:
                    raise

    def cfg(self, ping=True, timeout=5.0):
        """getcfg -> the controller's config and live state as a dict: sbus24, the axis limits (aMin, aMax, aRev), the
        stick channels (lx, ly, rx, ry), and sw, sl, tr, btn, lua with each control's channel ('c'), values and live
        position. It reaches USB only within 5 s of a ping (SBUSController.ino:1001-1029), so one goes first unless the
        caller has just pinged (ping=False). The line carries the controller's WiFi networks and passwords
        ('wifiNets'): it is parsed in memory, and the device's session.log line is redacted while it arrives
        (checkpoint.redact_text)."""
        if ping:
            self.ping()
        old = self.dev.log
        if old:
            self.dev.log = lambda name, direction, text: old(name, direction, redact_text(text))
        try:
            m = self.dev.mark()
            self.send({"t": "getcfg"})
            return json.loads(self._reply(r'^\{"e":"cfg"', m, timeout).string)
        finally:
            self.dev.log = old

    def bootlog(self, tries=1):
        """The controller's RTC boot record -> dict (buildBootLogJson SBUSController.ino:1696-1727): n counts boots since
        the last power loss (RTC_NOINIT) and up is this boot's millis(); rst/rstn and rtc0/rtcn name the reset. Not
        gated on a ping. Right after a boot one request went unanswered although pings were (run 20260924-112513, just
        after the controller rejoined WiFi), so a read after a reset asks twice (tries=2)."""
        for k in range(tries):
            self.ping()
            m = self.dev.mark()
            self.send({"t": "bootlog"})
            try:
                return json.loads(self._reply(r'^\{"e":"bootlog"', m, 5.0).string)
            except AssertionError:
                if k == tries - 1:
                    raise

    # ------------------------------------------------------------ controls (RAM only; SBUSController.ino:1098-1209)
    def axes(self, lx=0, ly=0, rx=0, ry=0):
        """The virtual sticks, -1..1 each; 0 is centre. A stick maps onto its channel between aMin and aMax (reversed
        by aRev), and ry/ly are negated so up is positive (:1098-1116)."""
        self.send({"t": "a", "lx": lx, "ly": ly, "rx": rx, "ry": ry})

    def switch(self, i, pos):
        """Switch i to position 0, 1 or 2; a 2-position or momentary switch's 1 reads as 0 (:1118-1135)."""
        self.send({"t": "sw", "i": i, "p": pos})

    def slider(self, i, pct):
        """Slider i to pct percent, clamped to 0-100: channel = (uint16)(pct/100 x 1639 + 172 + 0.5) (:1137-1150)."""
        self.send({"t": "sl", "i": i, "v": pct})

    def trim(self, i, d, pressed=None):
        """Trim i. A step-mode trim moves by its step for d=+1/-1 and centres for d=0; a button-mode trim sends valR
        (d>0) or valL while pressed and 992 once released, so `pressed` is given for those (:1152-1185)."""
        obj = {"t": "tr", "i": i, "d": d}
        if pressed is not None:
            obj["p"] = pressed
        self.send(obj)

    def button(self, i, pressed):
        """Physical button i: its value while pressed, 992 once released (:1187-1197)."""
        self.send({"t": "btn", "i": i, "p": pressed})

    def lua(self, i, pressed):
        """Lua (virtual) button i: its value while pressed, 992 once released (:1199-1209)."""
        self.send({"t": "lua", "i": i, "p": pressed})

    def center_all(self, cfg):
        """Release what the controller holds in RAM with no timeout: sticks centred, every button released, every
        button-mode trim released (the resume's clean-up after a cut-off sbus.* test, hil/resume.py). `cfg` is getcfg's
        dict. Switches, sliders and step-mode trims are left alone: their tests take the current position as the
        baseline. A released button writes 992 to its channel; on this bench every button and trim is on the matrix
        channel, so none shares a channel with a switch or slider."""
        self.axes(0, 0, 0, 0)
        for i, _ in enumerate(cfg.get("btn") or []):
            self.button(i, False)
        for i, tr in enumerate(cfg.get("tr") or []):
            if tr.get("m") == 1:
                self.trim(i, 1, False)

    def reassert_switches(self, cfg):
        """Send every switch to the position it holds (getcfg `cfg`'s pos): each writes its own channel again, which ends a
        "ch" override there (processCommandJson "sw"), and changes nothing where there was none. The resume's clean-up
        after a cut-off sbus.* test on an image with the INF8 verbs; sticks, buttons and trims are center_all's."""
        for i, s in enumerate(cfg.get("sw") or []):
            self.switch(i, s.get("pos", 0))

    # ------------------------------------------------------------ the INF8 test verbs (RAM only; NAVICORE.md INF8)
    def test_state(self, tries=2):
        """The probe: {"t":"flags"} with no value changes nothing and answers with the test state -> {'t', 'ok', 'flags',
        'stream', 'budget', 'sbus24', 'saved24'} (budget -1 = no frame limit; saved24 is the saved frame format, sbus24
        the live one), or None when the controller stays silent - an image without the verbs, where an unknown "t" falls
        through processCommandJson. A ping between tries releases a held reply (NUDGE_S); at most `tries` - 1 pings, so
        a silent image costs about `tries` seconds. Also what arms the verbs below."""
        m = self.dev.mark()
        self.send({"t": "flags"})
        for k in range(tries):
            try:
                self.verbs = json.loads(self.dev.expect(TEST_REPLY % "flags", timeout=self.NUDGE_S, since=m).string)
                return self.verbs
            except AssertionError:
                if k < tries - 1:
                    self.send({"t": "ping"})
        self.verbs = None
        return None

    def test_verb(self, obj, check=True, timeout=3.0):
        """One INF8 verb -> its reply as a dict (the test state after it). Refused with ValueError until a probe on this
        instance answered (test_state): "mode" without a working "save":false saves, so no verb goes to an image that
        has not shown it has them. check=True raises AssertionError when the reply says ok:false."""
        if self.verbs is None:
            raise ValueError("SbusCtl: probe the INF8 test verbs first (test_state()); an older image would save 'mode'")
        m = self.dev.mark()
        self.send(obj)
        st = json.loads(self._reply(TEST_REPLY % obj["t"], m, timeout).string)
        self.verbs = st
        if check and not st.get("ok"):
            raise AssertionError(f"the SBUS controller refused {json.dumps(obj, separators=(',', ':'))}: {st}")
        return st

    def flags(self, v):
        """Every frame's flags byte, 0-255 (0x04 lost frame, 0x08 failsafe; 0 is the controller's own)."""
        return self.test_verb({"t": "flags", "v": v})

    def stream(self, on, frames=None):
        """Stop (False) or restart the frames with no reset; with `frames`, exactly that many more (1-1000), then the
        stream stops again (the reply's budget counts down in loop())."""
        obj = {"t": "stream", "on": bool(on)}
        if frames is not None:
            obj["frames"] = frames
        return self.test_verb(obj)

    def sbus16(self, on):
        """SBUS-16 (on) or SBUS-24 for this boot only: "mode" with "save":false. The controller's channels reset and its
        controls re-apply as for a saved change (sticks and buttons to 992, a "ch" value gone); the saved mode, and so
        getcfg's sbus24, stay as they were."""
        return self.test_verb({"t": "mode", "sbus24": not on, "save": False})

    def glitch(self, kind, n):
        """One malformed burst in place of the next frame, sent even while the stream is off (glitch_bursts says what):
        truncate n bytes (1 to the frame length - 1), garbage n bytes (1-64, echoed as "hex"), double n frames (2-4), a
        gap of n ms (2-50) mid-frame, a dip of channel n to 992 for one frame."""
        if kind not in GLITCHES:
            raise ValueError(f"unknown glitch {kind!r} (one of {', '.join(GLITCHES)})")
        return self.test_verb({"t": "glitch", "kind": kind, "n": n})

    def channel(self, c, v):
        """A raw value (0-2047) on SBUS channel c (1-24), until a control writes that channel again."""
        return self.test_verb({"t": "ch", "c": c, "v": v})

    def has_route(self):
        """Whether the image has the second SBUS output and its "route" verb (SBUSController branch kyber-sbus): every
        test reply then names the route. Probes the verbs first when this instance has not."""
        st = self.verbs if self.verbs is not None else self.test_state()
        return bool(st) and "route" in st

    def route(self, to):
        """Which output carries the controls: "navicore" (output A, S5 - what a reset gives), "kyber" (output B, S4) or
        "both". One not routed gets no input: A carries the rest frame it took at boot (NaviCore stays linked and still),
        B sends nothing (the Kyber sees no radio). The flags, stream and glitch verbs act on the routed outputs."""
        if to not in ROUTES:
            raise ValueError(f"unknown route {to!r} (one of {', '.join(ROUTES)})")
        return self.test_verb({"t": "route", "to": to})

    def clear_faults(self):
        """Put the INF8 test state back to what a reset gives - flags 0, the stream on with no budget, the saved frame
        format, the controls routed to NaviCore - with no reset -> what it changed, as short strings ([] when nothing was
        off, or the image has no verbs: then only the probe went out). A "ch" value is not its business: it lasts until a
        control writes that channel (center_all, reassert_switches); a frame format put back resets the channels anyway."""
        st = self.test_state()
        if st is None:
            return []
        done = []
        if st.get("route", "navicore") != "navicore":
            self.route("navicore")
            done.append(f"the controls routed back to NaviCore (were {st['route']})")
        if st.get("flags"):
            self.flags(0)
            done.append(f"flags {st['flags']:#04x} -> 0")
        if not st.get("stream") or st.get("budget", -1) != -1:
            self.stream(True)
            done.append("the stream on again")
        if st.get("sbus24") != st.get("saved24"):
            self.sbus16(not st.get("saved24"))
            done.append(f"SBUS-{24 if st.get('saved24') else 16} again, as saved")
        return done

    def reset_rts(self, hold_s=0.2):
        """Reset the controller through its USB-Serial/JTAG port (serialdev.usb_jtag_reset), into its app: every
        control back to its boot value and SBUS silent for about a second. Its USB drops and comes back."""
        usb_jtag_reset(self.dev, hold_s)
