"""The SBUS controller (SBUSController on a WCB HW 3.2, the bench's `sbus` device) and an SBUS frame codec.
docs/hil_plan/NAVICORE.md §3 INF2.

The controller reads one JSON object per line on USB (processCommandJson, SBUSController.ino:1036-1360). Every line sent
here starts with '{': outside a JSON line single characters are commands, and 'm' and 'w' change and save its settings
(handleSerialCommands :1886-1900; docs/HIL_TESTING.md §1). Of the JSON verbs, 'mode', 'cfg' and 'wificfg' save too
(:1213-1225, :1238, :1352), so SbusCtl has no method for them. What it does send changes RAM only, with no timeout, and
lasts until the controller resets: a test puts back what it moved (center_all() for sticks, buttons and button-mode
trims). It transmits SBUS-24 every 9 ms with a constant flags byte 0x00 (:110-129, :944-955), and NaviCore decodes it
(NaviCore sbus_reader.h); every SBUS channel it moves, NaviCore re-emits on SBUS OUT (hil/servos.py).
"""
import json
import time

from .checkpoint import redact_text
from .navicore import entries
from .serialdev import usb_jtag_reset

SBUS_MIN, SBUS_CENTER, SBUS_MAX = 172, 992, 1811          # SBUSController.ino:113-115
HEADER, FOOTER = 0x0F, 0x00                               # :110-111; NaviCore sbus_reader.h checks both
FRAME_LEN = {16: 25, 24: 36}                              # 0x0F + 22 or 33 data bytes + flags + 0x00 (:122-125)
# The flags byte, just before the footer: bits 0-1 are the digital channels 17/18 of the 16-channel standard, and
# NaviCore reads 0x04 as a lost frame and 0x08 as failsafe (sbus_reader.h decodeFrame). The controller always sends 0.
FLAG_CH17, FLAG_CH18, FLAG_LOST, FLAG_FAILSAFE = 0x01, 0x02, 0x04, 0x08


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

    def __init__(self, dev):
        self.dev = dev

    def send(self, obj):
        """One JSON verb, compact, on one line. Never anything but a dict: see the module docstring."""
        self.dev.send(json.dumps(obj, separators=(",", ":")))

    # ------------------------------------------------------------ replies
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
            return json.loads(self.dev.expect(r'^\{"e":"cfg"', timeout=timeout, since=m).string)
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
                return json.loads(self.dev.expect(r'^\{"e":"bootlog"', timeout=5, since=m).string)
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

    def reset_rts(self, hold_s=0.2):
        """Reset the controller through its USB-Serial/JTAG port (serialdev.usb_jtag_reset), into its app: every
        control back to its boot value and SBUS silent for about a second. Its USB drops and comes back."""
        usb_jtag_reset(self.dev, hold_s)
