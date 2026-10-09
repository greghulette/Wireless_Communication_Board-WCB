"""Wires between probe headers and WCB ports: discovery, persistence, and channel allocation.

Nobody hand-maintains the wiring map. `discover()` makes each WCB port transmit in turn while
every probe counts edges on every header pin; the pin that toggles is the wire, and whether it
landed on the header's TX or RX pin says whether the cable is crossed or straight-through. The
result is saved to results/links.json and reused until the next `--discover`.

A probe has five channels: A/B hardware UARTs and C/D/E software serial. A link is bound to a
channel only while a test uses it; hardware channels go to links above SW_MAX_BAUD, and when a
probe runs out the least-recently-used link on it gives its channel up.

A probe can also be wired to NaviCore's own pins (docs/hil_plan/NAVICORE.md D-NC37, NC-WP14). Those wires are NcLinks,
keyed N<id><port> (N20S3, N20S4, N20S5, N20MAE, N20SBO), found by the second half of discover() and kept apart from the
WCB wires: LinkManager.nc_links, links.json "navicore_links". Everything that walks the WCB wires - all(), the GUI's
wiring diagram, the resume's wire checks, the checkpoint's links canon - never sees one; the probe bookkeeping (channels,
restarts, hand-offs) treats both alike.
"""
import json
import os
import re
import time
from datetime import datetime

from .checkpoint import atomic_write_text, redact_text
from .probe import HEADERS, HW_CHANNELS, HW_ONLY_HEADERS, SW_CHANNELS, restart_what
from .wcb import WCB

SW_MAX_BAUD = 38400

# ---------------------------------------------------------------- NaviCore's own pins (docs/hil_plan/NAVICORE.md D-NC37)
NC_PORTS = ("S3", "S4", "S5", "MAE", "SBO")     # its aux ports, the local Maestro bus (Serial2), SBUS OUT
NC_AUX = ("S3", "S4", "S5")                     # firmware numbering; the mesh calls them S1-S3 (rc_config.h:2030-2047)
NC_TAPS = ("MAE", "SBO")                        # a device already drives these lines: the probe only listens (RXONLY)
# The bauds D-NC37 wires for. Tests bind what GET_CONFIG auxBaud says at run time (nc_bauds); SBUS OUT is always 100000
# 8E2 inverted (sbus_reader.h:62; NaviCore.ino:4836-4846).
NC_DEFAULT_BAUD = {"S3": 115200, "S4": 9600, "S5": 9600, "MAE": 57600, "SBO": 100000}
NC_FORMAT = {"SBO": ("8E2", True)}              # (format, inverted); every other NaviCore pin is 8N1, not inverted
NC_KEY = re.compile(r"^N(\d+)(S[345]|MAE|SBO)$")
NAVICORE_ID = 20                                # the mesh id a key carries when NaviCore's own cannot be read
# SBUS OUT re-emits ~111 frames a second (the controller's 9 ms stream, teed byte for byte): thousands of edges in a quiet
# 0.3 s window. A pin with fewer is not SBUS OUT.
NC_SBUS_MIN_EDGES = 200
SBUS_HEADER, SBUS_FOOTER, SBUS_LENGTHS = 0x0F, 0x00, (25, 36)    # hil/sbus.py HEADER, FOOTER, FRAME_LEN


def nc_key(node, port):
    """'N20S3' for NaviCore id 20's port S3."""
    return f"N{node}{port}"


def nc_bauds(cfg):
    """{port: baud} for NaviCore's wired pins from GET_CONFIG `cfg`: auxBaud's S3, S4, S5 and maestro (rcSanBaud has
    clamped each into 1200-115200 on the way in, rc_config.h:1952-1960, so this is what the port runs), and SBUS OUT's
    fixed 100000. A value GET_CONFIG lacks keeps NC_DEFAULT_BAUD's."""
    ab = (cfg or {}).get("auxBaud") or {}
    out = dict(NC_DEFAULT_BAUD)
    for port, key in (("S3", "S3"), ("S4", "S4"), ("S5", "S5"), ("MAE", "maestro")):
        v = ab.get(key) if isinstance(ab, dict) else None
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out[port] = v
    return out


def sbus_run(data):
    """(frame length, offset, count) of the longest run of back-to-back SBUS frames in `data` - 25 or 36 bytes each, 0x0F
    first and 0x00 last, the next one starting right after - or (0, 0, 0). How discovery tells SBUS OUT from any other
    busy pin. A 0x0F inside a frame (byte 32 of this bench's resting frame, docs/HIL_TESTING.md §6) starts a run only
    by a coincidence that the next frame then breaks."""
    data = bytes(data)
    best = (0, 0, 0)
    for n in SBUS_LENGTHS:
        for i in range(len(data)):
            if data[i] != SBUS_HEADER:
                continue
            k = 0
            while (i + (k + 1) * n <= len(data) and data[i + k * n] == SBUS_HEADER
                   and data[i + (k + 1) * n - 1] == SBUS_FOOTER):
                k += 1
            if k > best[2]:
                best = (n, i, k)
    return best


def _line1(e):
    """The first line of an error, credentials hashed: NaviCore's ExpectTimeout tail can hold a CONFIG line."""
    text = str(e)
    return redact_text(text.splitlines()[0] if text else type(e).__name__)[:160]


class Link:
    navicore = False      # True on an NcLink: a wire on one of NaviCore's own pins

    def __init__(self, mgr, wcb, port, probe, header, swap, tap=False, verified=False):
        self.mgr = mgr
        self.wcb, self.port = wcb, port
        self.probe_name, self.header, self.swap = probe, header, swap
        self.tap, self.verified = tap, verified
        self.channel = None
        self.params = None
        self.bind_mark = None   # probe log mark just before this channel was bound (LinkManager.rebooted_since_bind)
        self.clean_to = None    # ...and how far past it the probe log is known to hold no restart
        self.lost = None        # the restart hit that unbound this channel, when another wire's bind forgot it first
        self.auto_baud = True
        self.last_used = 0.0
        self.pwm_active = False

    def __repr__(self):
        kind = "tap" if self.tap else "duplex"
        return f"W{self.wcb}{self.port} -> {self.probe_name} {self.header}{' SWAP' if self.swap else ''} ({kind})"

    @property
    def key(self):
        return f"W{self.wcb}{self.port}"

    @property
    def probe(self):
        return self.mgr.bench.probe(self.probe_name)

    def to_json(self):
        return dict(wcb=self.wcb, port=self.port, probe=self.probe_name, header=self.header,
                    swap=self.swap, tap=self.tap, verified=self.verified)

    # ------------------------------------------------------------ use
    def listen(self, baud=None, fmt="8N1", invert=False, hw=None):
        """Bind (or re-bind) this link. baud=None follows the WCB's configured baud for the port."""
        self.mgr.bind(self, baud, fmt, invert, hw)
        return self

    def default_baud(self):
        """The baud a bind with none given uses: the WCB's configured baud for the port (?BAUD,S<n>)."""
        return self.mgr.bench.port_baud(self.wcb, self.port)

    def _ch(self):
        if self.channel is not None and self.mgr.rebooted_since_bind(self):
            self.mgr.rebind_after_reboot(self)   # the probe restarted and forgot the channel; bind it again
        if self.channel is None:
            self.listen()
        self.last_used = time.monotonic()
        return self.channel

    def mark(self):
        self._ch()
        return self.probe.dev.mark()

    def send(self, data: bytes):
        if self.tap:
            raise AssertionError(f"{self.key} is a listen-only tap")
        self.probe.tx(self._ch(), data)

    def _since_ok(self, since):
        """The channel this link reads now, after checking it has owned it since `since`.

        When more wires on one probe need a hardware channel (A/B) than it has, bind() evicts the least recently
        used link, and the probe's RX history is kept per channel LETTER. Read across such a hand-off and you get
        another wire's bytes under this wire's name - on 2026-09-22 W2S2 'received' W2S5's broadcast and W2S5
        'missed' it, and the test blamed the firmware. Fail with the real reason instead.

        The same goes for a probe that restarted after the mark: the restart unbound the channel, so nothing was
        captured for this wire in between (2026-09-23: probe1 panic-looped when W1 rebooted, and two tests read an
        empty channel and blamed the firmware)."""
        if since is not None and self.channel is not None:
            hit = self.mgr.rebooted_since_bind(self)
            if hit is not None and hit[0] >= since:
                self.mgr.rebind_after_reboot(self)
                raise AssertionError(self._lost_words(hit))
        if since is not None and self.channel is None and self.lost is not None and self.lost[0] >= since:
            # Another wire on this probe noticed the restart first and forgot this one (forget_rebooted_probe). Report
            # the restart, not the hand-off the re-bind below would otherwise be blamed on.
            hit = self.lost
            self._ch()
            raise AssertionError(self._lost_words(hit))
        ch = self._ch()
        if since is not None:
            owner = self.mgr.handoff.get((self.probe_name, ch))
            if owner is not None and owner[1] > since:
                raise AssertionError(
                    f"{self.key}: its probe channel {ch} on {self.probe_name} was taken over after the mark, so the "
                    f"bytes since then are not this wire's - more wires on {self.probe_name} need a hardware "
                    f"channel (A/B) at once than it has; rewire so at most two of them do (docs/HIL_TESTING.md)")
        return ch

    def _lost_words(self, hit):
        return (f"{self.key}: {self.probe_name} {self.mgr.reboot_words(hit)} after the mark, so its channel was "
                f"unbound and nothing it received since then was captured")

    def bursts(self, since):
        return self.probe.bursts(self._since_ok(since), since)

    def received(self, since):
        return self.probe.received(self._since_ok(since), since)

    def time_of(self, marker: bytes, since):
        return self.probe.time_of(self._since_ok(since), marker, since)

    def errors(self, since):
        return self.probe.errors(self._since_ok(since), since)

    def expect(self, data: bytes, timeout=3.0, since=None):
        return self.probe.expect_bytes(self._since_ok(since), data, timeout=timeout, since=since)

    def expect_silence(self, window=1.5, since=None):
        return self.probe.expect_silence(self._since_ok(since), window=window, since=since)

    def rule(self, rule_id, pattern, reply: bytes, delay_ms=0, once=False):
        self.probe.rule_add(rule_id, self._ch(), pattern, reply, delay_ms=delay_ms, once=once)

    # ------------------------------------------------------------ pulses and levels
    # These use the header's pins directly, so the wire's serial channel is released first; the next
    # listen()/send() re-binds it. SWAP follows the cable: the pin facing the WCB's TX is the probe's
    # RX pin on a crossed cable and its TX pin on a straight-through one, and the reverse for output.
    def pwm_in(self):
        self.mgr.release(self)
        self.probe.pwm_in(self.header, swap=self.swap)
        self.pwm_active = True

    def pwm_out(self, us, hz=50):
        if self.tap:
            raise AssertionError(f"{self.key} is a listen-only tap")
        self.mgr.release(self)
        self.probe.pwm_out(self.header, us, swap=self.swap, hz=hz)
        self.pwm_active = True

    def pwm_stop(self):
        self.probe.pwm_in_off(self.header)
        self.probe.pwm_out_off(self.header)
        self.pwm_active = False

    def pulses(self, since):
        """[(width_us, count)] measured on this wire's WCB TX line since the probe mark."""
        return self.probe.pwm_pulses(since, self.header)

    def line_level(self):
        """Level of the WCB's TX line as the probe sees it (1 = idle high)."""
        self.mgr.release(self)
        return self.probe.level(self.header, "TX" if self.swap else "RX")

    def release(self):
        self.mgr.release(self)


class NcLink(Link):
    """A probe wire on one of NaviCore's own pins (NC_PORTS): the aux ports S3-S5, duplex (the probe reads what NaviCore
    writes and can type into its RX), and SBUS OUT and the local Maestro bus, listen-only taps. NaviCore's mesh id stands
    where a Link has its WCB number, so binding, the hand-off guard and probe restarts are Link's own. What differs: the
    key (N20S3, never W20S3), the baud a bind with none uses (what discovery last read from GET_CONFIG, else
    NC_DEFAULT_BAUD; the ncwire tests always bind the GET_CONFIG value themselves), and SBUS OUT's 8E2, inverted."""
    navicore = True

    def __init__(self, mgr, port, probe, header, swap, tap=None, verified=False, node=NAVICORE_ID):
        if port not in NC_PORTS:
            raise ValueError(f"not a NaviCore pin: {port} ({', '.join(NC_PORTS)})")
        super().__init__(mgr, int(node), port, probe, header, swap, tap=bool(tap) or port in NC_TAPS,
                         verified=verified)

    def __repr__(self):
        kind = "tap" if self.tap else "duplex"
        return f"{self.key} -> {self.probe_name} {self.header}{' SWAP' if self.swap else ''} ({kind})"

    @property
    def key(self):
        return nc_key(self.wcb, self.port)

    def to_json(self):
        return dict(node=self.wcb, port=self.port, probe=self.probe_name, header=self.header, swap=self.swap,
                    tap=self.tap, verified=self.verified)

    def listen(self, baud=None, fmt=None, invert=None, hw=None):
        """Bind (or re-bind) the wire; fmt and invert default to the pin's own (SBUS OUT: 8E2, inverted)."""
        dfmt, dinv = NC_FORMAT.get(self.port, ("8N1", False))
        self.mgr.bind(self, baud, fmt or dfmt, dinv if invert is None else invert, hw)
        return self

    def default_baud(self):
        return self.mgr.nc_baud.get(self.port, NC_DEFAULT_BAUD[self.port])


class LinkManager:
    def __init__(self, bench, path):
        self.bench = bench
        self.path = path
        self.links = {}    # (wcb, port) -> Link
        self.owners = {}   # probe name -> {channel: Link}
        # (probe name, channel) -> (link key that owns it now, probe log mark when it took the channel over from
        # a DIFFERENT link). A read whose `since` is older than that hand-off would return bytes the probe captured
        # for another wire under this wire's name - see Link._since_ok().
        self.handoff = {}
        self.discovered_at = None
        self.nc_links = {}  # NaviCore port (NC_PORTS) -> NcLink
        self.nc_baud = {}   # NaviCore port -> baud from discovery's GET_CONFIG (NcLink.default_baud)

    # ------------------------------------------------------------ persistence
    def load(self):
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except ValueError as e:
            # A corrupt links.json (a power-off mid-write before saves were atomic) must not stop Bench() - the GUI
            # would not open. Keep it aside for a look, and start with no wires.
            bad = self.path + ".corrupt"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            note = getattr(self.bench, "note", None)
            msg = f"links.json was unreadable ({e}) - moved to {bad}; the bench starts with no wires"
            try:
                note(msg) if note else print(msg)
            except Exception:  # noqa: BLE001 - no session log open yet
                print(msg)
            self.links = {}
            self.nc_links = {}
            return False
        self.links = {}
        for d in data.get("links", []):
            if d["probe"] in self.bench.cfg["devices"]:
                link = Link(self, **d)
                if self.device_on(link.wcb, link.port) is not None:
                    link.tap = True   # a links.json older than the device must not stay transmit-capable
                self.links[(link.wcb, link.port)] = link
        self.nc_links = {}
        for d in data.get("navicore_links") or []:
            try:
                if d["probe"] in self.bench.cfg["devices"] and d["port"] in NC_PORTS:
                    link = NcLink(self, d["port"], d["probe"], d["header"], bool(d.get("swap", False)),
                                  tap=d.get("tap"), verified=bool(d.get("verified", False)),
                                  node=d.get("node", NAVICORE_ID))
                    self.nc_links[link.port] = link
            except (KeyError, TypeError, ValueError):
                continue        # a malformed row costs only itself
        self.discovered_at = data.get("discovered_at")
        return True

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Atomic: a power-off mid-write used to leave a truncated links.json, and Bench() then refused to load.
        data = {"discovered_at": self.discovered_at, "links": [l.to_json() for l in self.links.values()]}
        if self.nc_links:   # only then: a bench with no probe on NaviCore keeps the links.json it always had
            data["navicore_links"] = [l.to_json() for l in self.nc_links.values()]
        atomic_write_text(self.path, json.dumps(data, indent=2))

    def restore(self, canon):
        """Rebuild the wires from a checkpoint's links canon (links.json missing on a resume, and the user said to
        restore it). Every wire comes back unverified; the resume checks then verify each one."""
        self.links = {}
        for d in canon:
            if d["probe"] in self.bench.cfg["devices"]:
                link = Link(self, d["wcb"], d["port"], d["probe"], d["header"], swap=d.get("swap", False),
                            tap=d.get("tap", False) or self.device_on(d["wcb"], d["port"]) is not None)
                self.links[(link.wcb, link.port)] = link
        self.save()

    # ------------------------------------------------------------ lookup
    def get(self, wcb, port, raw=False):
        """The wire on W<wcb> <port>, or None. A port with a real device and no port_stimulus is hidden from tests —
        nothing may be sent there — even when a listen-only wire is on it; so is a tap on what a device sends INTO the
        port (device_sends), which never carries the port's output. raw=True (GUI, plan, devchecks) returns it anyway."""
        link = self.links.get((wcb, port))
        if link is not None and not raw and (self.device_only(wcb, port) or self.device_sends(wcb, port)):
            return None
        return link

    def usable(self, wcb, port, send=False):
        """For a test that picks a port at run time and puts traffic on it: the wire, unless the port has any real
        device (a port_stimulus covers only its own safe command) or the probe must send and the wire is a tap."""
        link = self.links.get((wcb, port))
        if link is None or self.device_on(wcb, port) is not None or (send and link.tap):
            return None
        return link

    def require(self, wcb, port):
        link = self.get(wcb, port)
        if link is None:
            from .runner import Skip, describe_device
            if self.device_only(wcb, port):
                raise Skip(f"W{wcb}{port} has {describe_device(self.device_on(wcb, port))} on it, so no test traffic goes there")
            if self.device_sends(wcb, port):
                raise Skip(f"W{wcb}{port} has {describe_device(self.device_on(wcb, port))} on it, and its probe wire only "
                           f"hears what that device sends into the port, never the port's output")
            raise Skip(f"no wire on W{wcb} {port} (run with --discover after wiring)")
        return link

    def all(self, duplex_only=False):
        """The WCB wires (never NaviCore's: nc_all)."""
        return [l for l in self.links.values() if not (duplex_only and l.tap)]

    def get_key(self, key):
        """The wire a test's port key names - 'W1S3' as get() finds it (a device-only port is hidden) or 'N20S3' as
        nc_get() does - or None."""
        m = NC_KEY.match(key)
        if m:
            return self.nc_get(m.group(2), int(m.group(1)))
        m = re.match(r"^W(\d+)(S[1-5])$", key)
        return self.get(int(m.group(1)), m.group(2)) if m else None

    def nc_get(self, port, node=None):
        """NaviCore's wire on `port` (NC_PORTS), or None; with `node`, only when NaviCore had that mesh id when the wire
        was found."""
        link = self.nc_links.get(port)
        return link if link is not None and (node is None or link.wcb == node) else None

    def nc_require(self, port):
        """NaviCore's wire on `port`, or Skip naming the wiring it needs."""
        link = self.nc_get(port)
        if link is None:
            from .runner import Skip
            raise Skip(f"no wire on NaviCore's {port} ({nc_key(NAVICORE_ID, port)}): wire probe 3 as "
                       f"docs/hil_plan/NAVICORE.md D-NC37 has it, then run --discover")
        return link

    def nc_all(self):
        """NaviCore's wires."""
        return list(self.nc_links.values())

    def _every(self):
        """The WCB wires and NaviCore's: everything a probe's channel bookkeeping walks."""
        return list(self.links.values()) + list(self.nc_links.values())

    # ------------------------------------------------------------ real devices on WCB ports
    def device_on(self, wcb, port):
        """The real device bench.json "port_devices" says is wired to this WCB port, or None."""
        return self.bench.cfg.get("port_devices", {}).get(f"W{wcb}{port}")

    def device_only(self, wcb, port):
        """A real device is on the port and no port_stimulus names a safe way to make the port transmit, so the
        tool must not make it transmit at all (discovery, verify and link.out skip it)."""
        return self.device_on(wcb, port) is not None and f"W{wcb}{port}" not in self.bench.cfg.get("port_stimulus", {})

    def device_sends(self, wcb, port):
        """The port's port_stimulus makes a device send INTO it ({"kyber_button": n}): the wire there taps that device's
        TX, not the WCB port's output. Discovery, verify, link.out and devchecks use it; get() hides it from tests, which
        all expect a wire to carry the port's output (input.softserial_idle_high_after_boot read W3 S5 empty in full
        run 20261009-174225 the day the Kyber's MarcDuino tap was added)."""
        return "kyber_button" in (self.bench.cfg.get("port_stimulus", {}).get(f"W{wcb}{port}") or {})

    # ------------------------------------------------------------ channels
    def _owned(self, probe_name):
        return self.owners.setdefault(probe_name, {})

    def forget_probe(self, probe_name):
        """The probe was reset or closed: none of its channels are bound any more."""
        # Cleared in place, never popped: bind() holds a reference to this table while it binds, and
        # the first use of a probe resets it and lands here (Bench.probe). A popped table would leave
        # bind() registering into an orphan, so the next link would be handed the same channel and
        # silently re-point it at its own header — the first link then reads another port's traffic.
        self.owners.setdefault(probe_name, {}).clear()
        for key in [k for k in self.handoff if k[0] == probe_name]:
            del self.handoff[key]
        for link in self._every():
            if link.probe_name == probe_name:
                link.channel = None
                link.params = None
                link.bind_mark = link.clean_to = link.lost = None   # a reopened device numbers its lines from 0

    # ------------------------------------------------------------ probe restarts
    def rebooted_since_bind(self, link):
        """(line index, monotonic time, text) of the first boot, panic or port-reopen line the link's probe printed
        since the link's channel was bound, or None. Any restart counts, planned or not: either way the probe forgot
        every channel (probe.py REBOOT_MARKERS)."""
        if link.channel is None or link.bind_mark is None:
            return None
        probe = self.bench.probes.get(link.probe_name)
        if probe is None:
            return None
        # Scan only what is new since the last clean scan: tests poll reads every 50 ms, and a wire can stay bound
        # (auto-baud) for a whole run's worth of probe lines.
        end = probe.dev.mark()
        hits = probe.reboots(max(link.bind_mark, link.clean_to or 0), end)
        if hits:
            return hits[0]
        link.clean_to = end
        return None

    def reboot_words(self, hit):
        """'panicked at t=12.345 (Guru Meditation ...)' / 'rebooted at t=...' / 'lost its USB port at t=...', in the
        session log's time scale."""
        _, t, text = hit
        what = restart_what(text)
        return f"{what} at t={self.bench.log_time(t):.3f} ({text.strip(chr(0)).strip()[:90]})"

    def forget_rebooted_probe(self, probe_name):
        """The probe restarted (or its port re-enumerated): drop the bindings that restart destroyed, and ONLY those.

        A link bound after the restart is live on the probe and keeps its channel. Forgetting it too (as this once did,
        wholesale) freed its letter on the host while the probe still held its header's pins, so the next bind picked
        that letter for another header, the probe answered 'ERR pin in use' (probe_main.cpp bindChannel unbinds only the
        channel it is given), and every test that bound those wires in that order failed.

        Each stale channel also gets a best-effort UNBIND. After a real restart it is a no-op ('OK UNBIND' on an unbound
        channel); after a USB re-enumeration with no reset (probe.py REBOOT_MARKERS '<<reopened') the probe still holds
        it, and must let go before the host hands the letter or the header to another wire. The first UNBIND that fails
        (a probe still panic-looping) ends the attempts: each would wait out the command timeout, and a restarted probe
        holds nothing anyway."""
        probe = self.bench.probes.get(probe_name)
        stale = [(l, self.rebooted_since_bind(l)) for l in self._every()
                 if l.probe_name == probe_name and l.channel is not None]
        stale = [(l, hit) for l, hit in stale if hit is not None]
        owned = self._owned(probe_name)
        tell_probe = probe is not None
        for l, hit in stale:
            ch = l.channel
            if tell_probe:
                try:
                    probe.unbind(ch)
                except Exception as e:  # noqa: BLE001 - best effort; see above
                    tell_probe = False
                    self.bench.note(f"{probe_name}: UNBIND {ch} after its restart failed ({e}) - not sending more")
            owned.pop(ch, None)
            self.handoff.pop((probe_name, ch), None)
            if probe is not None:
                probe.bound.pop(ch, None)
            l.channel = l.params = l.bind_mark = l.clean_to = None
            l.lost = hit                    # a read across the restart still fails with the reason (Link._since_ok)

    def rebind_after_reboot(self, link):
        """Bind `link` again, with the parameters it had, after its probe restarted and forgot it."""
        hit = self.rebooted_since_bind(link)
        baud, fmt, invert = link.params
        was_hw = link.channel in HW_CHANNELS
        auto = link.auto_baud
        if hit is not None:
            self.bench.note(f"{link.probe_name} {self.reboot_words(hit)} since {link.key} was bound - "
                            f"forgetting its channels and binding {link.key} again")
        self.forget_rebooted_probe(link.probe_name)
        self.bind(link, None if auto else baud, fmt, invert, True if was_hw else None)

    # ------------------------------------------------------------ manual wiring (GUI)
    def set_link(self, wcb, port, probe, header, swap=False, tap=False):
        tap = tap or self.device_on(wcb, port) is not None   # never transmit into a real device's line
        old = self.links.get((wcb, port))
        if old:
            self.release(old)
        for key, l in list(self.links.items()):          # one wire per probe header
            if l.probe_name == probe and l.header == header and key != (wcb, port):
                self.release(l)
                del self.links[key]
        for p, l in list(self.nc_links.items()):         # ...a NaviCore pin's included
            if l.probe_name == probe and l.header == header:
                self.release(l)
                del self.nc_links[p]
        link = Link(self, wcb, port, probe, header, swap=swap, tap=tap)
        self.links[(wcb, port)] = link
        self.save()
        return link

    def set_nc_link(self, port, probe, header, swap=False, tap=None, node=NAVICORE_ID):
        """Wire NaviCore's `port` to a probe header by hand: one wire per header, as set_link; MAE and SBO are always
        listen-only. Discovery normally finds these (discover)."""
        if port not in NC_PORTS:
            raise ValueError(f"not a NaviCore pin: {port} ({', '.join(NC_PORTS)})")
        old = self.nc_links.get(port)
        if old:
            self.release(old)
        for key, l in list(self.links.items()):
            if l.probe_name == probe and l.header == header:
                self.release(l)
                del self.links[key]
        for p, l in list(self.nc_links.items()):
            if p != port and l.probe_name == probe and l.header == header:
                self.release(l)
                del self.nc_links[p]
        link = NcLink(self, port, probe, header, swap, tap=tap, node=node)
        self.nc_links[port] = link
        self.save()
        return link

    def remove_nc_link(self, port):
        link = self.nc_links.pop(port, None)
        if link:
            self.release(link)
            self.save()

    def remove_link(self, wcb, port):
        link = self.links.pop((wcb, port), None)
        if link:
            self.release(link)
            self.save()

    def verify(self, link, try_swap=True):
        """Make the WCB port transmit and check the bytes; on silence, try the other orientation. check() sends the same
        stimulus for the resume checks - keep the two in step."""
        if self.device_only(link.wcb, link.port):
            self.bench.note(f"{link.key}: a real device is on this port and bench.json has no port_stimulus for it, "
                            f"so nothing is sent to verify the probe wire")
            link.verified = False
            self.save()
            return False
        for attempt in ((link.swap, not link.swap) if try_swap else (link.swap,)):
            self.release(link)
            link.swap = attempt
            console, cmd, expected = self.stimulus(link.wcb, link.port)
            try:
                m = link.mark()
                self.fire(console, cmd)
                link.expect(expected, timeout=3, since=m)
                link.verified = True
                break
            except AssertionError:
                link.verified = False
            finally:
                self.release(link)
        self.save()
        return link.verified

    def check(self, link, swap=None):
        """Does the wire still carry bytes in one orientation (the recorded one, or `swap`)? The resume checks use it.
        The same stimulus as verify() - keep the two in step - at the WCB's configured baud, but it changes nothing:
        link.swap is put back, `verified` is left alone, the channel is released and nothing is saved."""
        if self.device_only(link.wcb, link.port):
            return False
        keep = link.swap
        try:
            try:
                self.release(link)
            except Exception:  # noqa: BLE001 - a probe that was reset since; the bind below reports a real problem
                link.channel, link.params = None, None
            if swap is not None:
                link.swap = swap
            console, cmd, expected = self.stimulus(link.wcb, link.port)
            try:
                m = link.mark()
                self.fire(console, cmd)
                link.expect(expected, timeout=3, since=m)
                return True
            except AssertionError:
                return False
            finally:
                try:
                    self.release(link)
                except Exception:  # noqa: BLE001
                    link.channel, link.params = None, None
        finally:
            link.swap = keep

    def release(self, link):
        if link.channel is None:
            return
        owned = self._owned(link.probe_name)
        try:
            link.probe.unbind(link.channel)
        finally:
            owned.pop(link.channel, None)
            link.channel = None
            link.params = None
            link.bind_mark = link.clean_to = None

    def release_all(self):
        for link in list(self.links.values()):
            self.release(link)

    def bind(self, link, baud, fmt, invert, hw):
        probe = link.probe          # resolved before the channel table is read: the first use of a
                                    # probe resets it and clears that table (see forget_probe)
        link.auto_baud = baud is None
        if baud is None:
            baud = link.default_baud()
        params = (baud, fmt, invert)
        if link.header in HW_ONLY_HEADERS:
            if hw is False:
                raise AssertionError(f"{link.probe_name} header {link.header} can only use a hardware channel")
            want_hw = True
        else:
            want_hw = hw if hw is not None else baud > SW_MAX_BAUD
        if link.channel is not None and self.rebooted_since_bind(link):
            # The cached binding is gone on the probe: it restarted (a panic, or a planned MESH LEAVE) since this
            # link was bound. Returning early here is how a stale channel C was read empty after probe1 panicked.
            self.bench.note(f"{link.probe_name} {self.reboot_words(self.rebooted_since_bind(link))} since {link.key} "
                            f"was bound - forgetting its channels")
            self.forget_rebooted_probe(link.probe_name)
        if link.channel is not None and link.params == params and (link.channel in HW_CHANNELS) == want_hw:
            link.last_used = time.monotonic()
            return
        self.release(link)
        if link.pwm_active:
            link.pwm_stop()
        owned = self._owned(link.probe_name)
        if want_hw:
            pool = list(HW_CHANNELS)
        elif hw is False:
            pool = list(SW_CHANNELS)
        else:
            pool = list(SW_CHANNELS) + list(HW_CHANNELS)
        ch = next((c for c in pool if c not in owned), None)
        if ch is None:
            victims = sorted((l for c, l in owned.items() if c in pool), key=lambda l: l.last_used)
            if not victims:
                raise AssertionError(f"{link.probe_name}: no free channel for {link.key}")
            ch = victims[0].channel
            self.release(victims[0])
        prev = self.handoff.get((link.probe_name, ch))
        if prev is None or prev[0] != link.key:
            # The channel changes OWNER (first use, or taken from another wire). Everything the probe logged on it
            # before this point belongs to someone else. Re-binding the SAME link (a baud change, resync) is not a
            # hand-off and keeps its mark, so an auto-baud re-bind mid-test does not trip the guard.
            self.handoff[(link.probe_name, ch)] = (link.key, probe.dev.mark())
        bind_mark = probe.dev.mark()        # before the BIND: a restart from here on unbinds it again
        probe.bind(ch, link.header, baud, fmt=fmt, invert=invert, swap=link.swap, rx_only=link.tap)
        for other in self._every():         # belt and braces: one channel, one link
            if other is not link and other.probe_name == link.probe_name and other.channel == ch:
                other.channel, other.params, other.bind_mark, other.clean_to = None, None, None, None
        owned[ch] = link
        link.channel, link.params, link.last_used = ch, params, time.monotonic()
        link.bind_mark, link.clean_to, link.lost = bind_mark, None, None

    def resync(self, wcb=None):
        """Re-bind every active auto-baud link after a WCB's port bauds changed."""
        for w in ([wcb] if wcb else list({l.wcb for l in self.links.values()})):
            self.bench.invalidate(w)
        for link in list(self.links.values()):
            if link.channel is not None and link.auto_baud and (wcb is None or link.wcb == wcb):
                _, fmt, invert = link.params
                was_hw = link.channel in HW_CHANNELS
                link.params = None
                self.bind(link, None, fmt, invert, True if was_hw else None)

    # ------------------------------------------------------------ discovery
    def console_for(self, wcb):
        """The console that drives W<wcb>: its own USB cable if it has one that opens, else wcb1."""
        name = self.bench.usb_wcbs().get(wcb)
        if name and name != "wcb1":
            try:
                self.bench.dev(name)
                return name
            except Exception:
                pass
        return "wcb1"

    def stimulus(self, wcb, port, text=None):
        """(console device, command, bytes expected on the wire) that make W<wcb> <port> transmit.
        A WCB with its own USB cable is driven there with a local ;S; any other goes through wcb1
        as ;W<n>;S over the mesh. Ports with a real device use bench.json's port_stimulus: a console command and the
        bytes it puts on the wire ({"send", "expect"}), or a device that sends INTO the port - {"kyber_button": n}, the
        real Kyber's pad button n pressed through the SBUS controller, whose MarcDuino line is what a tap on the port
        hears (hil/devchecks.kyber_press). For that kind the console is None and the command a callable: fire() runs
        either."""
        console = self.console_for(wcb)
        override = self.bench.cfg.get("port_stimulus", {}).get(f"W{wcb}{port}")
        if override and "kyber_button" in override:
            from .devchecks import kyber_press
            action, expected = kyber_press(self.bench, int(override["kyber_button"]))
            return None, action, expected
        if override:
            return console, override["send"], bytes.fromhex(override["expect"])
        payload = text or "U" * 16   # 0x55: an edge on every bit
        n = port[1]
        local = console != "wcb1" or wcb == self.bench.usb_wcb_number()
        cmd = f";S{n}{payload}" if local else f";W{wcb};S{n}{payload}"
        return console, cmd, payload.encode() + b"\r"

    def fire(self, console, cmd):
        """Send a stimulus() pair: `cmd` typed on WCB `console`, or, with console None, the device action `cmd` run."""
        if console is None:
            cmd()
        else:
            WCB(self.bench.dev(console)).send(cmd)

    def discover(self, log=print):
        """Find every wire: each WCB port in turn (below), then NaviCore's own pins (_discover_navicore). The WCB half
        is what it always was; a pin busy in a baseline window is noted once, with its highest count, instead of once
        per port swept (SBUS OUT on a probe header toggles through every window). -> every wire found, WCB wires
        first."""
        bench = self.bench
        probes = {n: bench.probe(n) for n in bench.probe_names()}
        prior_nc = dict(self.nc_links)
        for l in prior_nc.values():          # the probes are reset below: no channel survives the sweep
            l.channel = l.params = l.bind_mark = l.clean_to = None
        # Every WCB — the main console, any other USB-attached WCB, and mesh-only ones. (Building
        # this from mesh_only_wcbs alone silently skipped a WCB the moment it got a USB cable.)
        ports = [(w, p) for w in bench.wcb_numbers() for p in HEADERS]
        taps = {(t["wcb"], t["port"]) for t in bench.cfg.get("taps", [])}
        taps |= {(int(m.group(1)), m.group(2)) for m in (re.match(r"^W(\d+)(S[1-5])$", k)
                                                        for k in bench.cfg.get("port_devices", {})) if m}
        skip = set(bench.cfg.get("discover_skip", []))

        prior = dict(self.links)   # wires on ports the sweep must not touch are put back afterwards
        self.owners = {}
        self.links = {}
        for p in probes.values():
            p.reset()
            p.edges_start()
        found, notes, noisy = {}, [], {}
        try:
            for w, port in ports:
                if f"W{w}{port}" in skip or self.device_only(w, port):
                    if self.device_only(w, port):
                        notes.append(f"W{w}{port}: skipped — {self.device_on(w, port).get('kind', 'a device')} on it and no port_stimulus")
                    continue
                try:
                    console, cmd, _ = self.stimulus(w, port)
                except AssertionError as e:          # a device stimulus that cannot run here (no Kyber config)
                    notes.append(f"W{w}{port}: skipped — its port_stimulus cannot run: {e}")
                    continue
                time.sleep(0.1)
                for p in probes.values():
                    p.edges_read()
                time.sleep(0.3)
                base = {n: p.edges_read() for n, p in probes.items()}
                self.fire(console, cmd)
                over_mesh = console == "wcb1" and w != bench.usb_wcb_number()
                time.sleep(0.9 if over_mesh else 0.45)
                hit = {n: p.edges_read() for n, p in probes.items()}
                cands = []
                for n in probes:
                    for h in HEADERS:
                        for which in ("tx", "rx"):
                            v, b = hit[n][h][which], base[n][h][which]
                            # A device's stimulus (console None: the Kyber's pad) runs for seconds with the SBUS
                            # controller routed to it; only a header no wire took yet can be its tap.
                            claimed = console is None and (n, h) in {(l.probe_name, l.header) for l in found.values()}
                            if isinstance(v, int) and isinstance(b, int) and v >= 6 and b == 0 and not claimed:
                                cands.append((v, n, h, which))
                            elif b == "storm" or (isinstance(b, int) and b > 0):
                                prev = noisy.get((n, h, which))
                                if b == "storm" or not isinstance(prev, int) or b > prev:
                                    noisy[(n, h, which)] = b if prev != "storm" else prev
                if not cands:
                    continue
                cands.sort(reverse=True)
                if len(cands) > 1:
                    notes.append(f"W{w}{port}: several pins toggled {[c[1:] for c in cands]} — using the busiest")
                _, n, h, which = cands[0]
                found[(w, port)] = Link(self, w, port, n, h, swap=(which == "tx"), tap=(w, port) in taps)
        finally:
            for p in probes.values():
                p.edges_stop()
        notes += [f"{n} {h}.{which} noisy (baseline {b})" for (n, h, which), b in noisy.items()]

        self.links = found
        for link in self.links.values():
            console, cmd, expected = self.stimulus(link.wcb, link.port)
            try:
                m = link.mark()
                self.fire(console, cmd)
                link.expect(expected, timeout=3, since=m)
                link.verified = True
            except AssertionError as e:
                link.verified = False
                notes.append(f"{link.key}: wire found but bytes did not verify at the configured baud — {e}")
            finally:
                self.release(link)
        # A device-only or discover_skip port is never swept, so a wire added there by hand would otherwise vanish.
        taken = {(l.probe_name, l.header) for l in self.links.values()}
        for (w, port), old in prior.items():
            if (w, port) in self.links or not (self.device_only(w, port) or f"W{w}{port}" in skip):
                continue
            if (old.probe_name, old.header) in taken:
                notes.append(f"{old.key}: dropped the wire added by hand — {old.probe_name} {old.header} now belongs to another port")
                continue
            self.links[(w, port)] = Link(self, w, port, old.probe_name, old.header, swap=old.swap,
                                         tap=old.tap or self.device_on(w, port) is not None, verified=old.verified)
            taken.add((old.probe_name, old.header))
            notes.append(f"W{w}{port}: kept the wire added by hand (auto-detect does not sweep this port)")
        nc_found = self._discover_navicore(probes, notes, prior_nc)
        self.nc_links = prior_nc if nc_found is None else nc_found
        self.discovered_at = datetime.now().isoformat(timespec="seconds")
        self.save()
        for note in notes:
            log(f"  note: {note}")
        return list(self.links.values()) + list(self.nc_links.values())

    def _discover_navicore(self, probes, notes, prior):
        """The NaviCore half of discover() -> {port: NcLink} found, each checked by its own bytes, or None when the half
        could not run (NaviCore not answering, no probe header left): the NaviCore wires found before are kept then.

        It runs on the probe headers no WCB wire took, so it needs a free one: on a bench whose probes all serve WCB
        ports it sends NaviCore nothing. GET_CONFIG gives the bauds, the local Maestro slots and what is routed to
        S3-S5; it is never noted (it holds the mesh and access point passwords, which Bench.log hashes on NaviCore's
        lines). With every probe counting edges on its free pins:
          - S3, S4, S5: a TEST_ACTION serial action of sixteen 'U' (0x55, an edge on every bit) out that port, the kind
            of plain text navicore.serial_route_dbg puts there every run (RA_SERIAL, NaviCore.ino:2221-2240). A port an
            HCR, MP3 Trigger, DFPlayer or WLED is routed to is left alone (NaviCore.local_devices): it would read them.
          - MAE: ?MAE,GET,<slot>,0 on the first local Maestro slot, a read that puts AA <device> 10 00 on the Maestro
            bus and moves nothing (maestroLocalQuery and maestroWrite, NaviCore.ino:692-787).
          - SBO: nothing is sent. SBUS OUT re-emits the controller's stream while sbusOutEnabled is on (the byte tee,
            sbus_reader.h:89-93), so its pin toggles through every quiet window; the busiest such pin that carries
            back-to-back SBUS frames to a hardware channel at 100000 8E2 inverted (sbus_run) is the wire.
        As for a WCB port, the pin that stayed quiet in its baseline window and toggled during the stimulus is the wire,
        the busiest one when several did, and a hit on the header's TX pin means a straight-through cable (SWAP). Each
        wire is then confirmed by its exact bytes at GET_CONFIG's baud: the 'U' line and its CR, or the Maestro frame. A
        pin not swept this time (a device routed there, no local Maestro slot, SBUS OUT off) keeps the wire found
        before, if its header is still free."""
        bench = self.bench
        if not bench.has("navicore") or not probes:
            return {}
        taken = {(l.probe_name, l.header) for l in self.links.values()}
        free = [(n, h) for n in probes for h in HEADERS if (n, h) not in taken]
        if not free:
            notes.append("NaviCore's pins were not swept: every probe header serves a WCB port (D-NC37 wires a third "
                         "probe to them)")
            return None
        from .navicore import NaviCore
        try:
            nc = NaviCore(bench.dev("navicore"))
            nc.ping()
            cfg = nc.config()
        except Exception as e:  # noqa: BLE001 - a NaviCore that does not answer costs only its own wires
            notes.append(f"NaviCore did not answer ({_line1(e)}): its pins were not swept, and the NaviCore wires "
                         f"found before are kept")
            return None
        node = int((cfg.get("wcbNetwork") or {}).get("deviceId") or NAVICORE_ID)
        bauds = nc_bauds(cfg)
        self.nc_baud = dict(bauds)
        owned = {d.rsplit(" on ", 1)[-1] for d in NaviCore.local_devices(cfg)}
        slots = NaviCore.local_slots(cfg)
        found, quiet, swept = {}, {}, []

        def window():
            """Edge counts over a quiet 0.3 s; each free pin's lowest count across windows goes into `quiet`."""
            time.sleep(0.1)
            for p in probes.values():
                p.edges_read()
            time.sleep(0.3)
            base = {n: p.edges_read() for n, p in probes.items()}
            for n, h in free:
                for which in ("tx", "rx"):
                    b = base[n][h][which]
                    c = b if isinstance(b, int) else 0          # 'busy' or 'storm': not an SBUS candidate
                    quiet[(n, h, which)] = min(quiet.get((n, h, which), c), c)
            return base

        for p in probes.values():
            p.edges_start()
        try:
            window()
            for port in NC_AUX + ("MAE",):
                if port in owned:
                    notes.append(f"NaviCore's {port} was not swept: a device is routed to it, and it would read the "
                                 f"stimulus")
                    continue
                if port == "MAE" and not slots:
                    notes.append("NaviCore's MAE was not swept: it has no local Maestro slot, so nothing writes its "
                                 "Maestro bus")
                    continue
                swept.append(port)
                base = window()
                try:
                    if port == "MAE":
                        nc.cli(f"?MAE,GET,{slots[0][0]},0")
                    else:
                        nc.test_action({"type": "serial", "port": port, "cmd": "U" * 16})
                except AssertionError as e:
                    notes.append(f"NaviCore's {port}: the stimulus failed ({_line1(e)})")
                    continue
                time.sleep(0.45)
                hit = {n: p.edges_read() for n, p in probes.items()}
                used = {(l.probe_name, l.header) for l in found.values()}
                cands = []
                for n, h in free:
                    if (n, h) in used:
                        continue
                    for which in ("tx", "rx"):
                        v, b = hit[n][h][which], base[n][h][which]
                        if isinstance(v, int) and isinstance(b, int) and v >= 6 and b == 0:
                            cands.append((v, n, h, which))
                if not cands:
                    continue
                cands.sort(reverse=True)
                if len(cands) > 1:
                    notes.append(f"NaviCore's {port}: several pins toggled {[c[1:] for c in cands]} - using the busiest")
                _, n, h, which = cands[0]
                found[port] = NcLink(self, port, n, h, which == "tx", node=node)
        finally:
            for p in probes.values():
                p.edges_stop()

        if not cfg.get("sbusOutEnabled"):
            notes.append("NaviCore's SBO was not swept: sbusOutEnabled is off, so nothing transmits on SBUS OUT")
        else:
            swept.append("SBO")
            used = {(l.probe_name, l.header) for l in found.values()}
            cands = sorted(((c, n, h, w) for (n, h, w), c in quiet.items()
                            if c >= NC_SBUS_MIN_EDGES and (n, h) not in used), reverse=True)
            for c, n, h, which in cands[:4]:
                trial = NcLink(self, "SBO", n, h, which == "tx", node=node)
                run = (0, 0, 0)
                try:
                    trial.listen(hw=True)
                    m = trial.mark()
                    time.sleep(0.35)
                    run = sbus_run(trial.received(m))
                except AssertionError as e:
                    notes.append(f"NaviCore's SBO: {n} {h} could not be read at 100000 8E2 inverted ({_line1(e)})")
                finally:
                    try:
                        self.release(trial)
                    except Exception:  # noqa: BLE001 - a probe gone since; the next bind reports it
                        trial.channel = trial.params = None
                if run[2] >= 3:
                    trial.verified = True
                    found["SBO"] = trial
                    break
            else:
                notes.append("NaviCore's SBO: no free pin carried back-to-back SBUS frames at 100000 8E2 inverted"
                             if cands else "NaviCore's SBO: no free pin toggled through every quiet window")

        for port, link in found.items():
            if port == "SBO":
                continue
            try:
                link.listen(bauds[port])
                m = link.mark()
                if port == "MAE":
                    nc.cli(f"?MAE,GET,{slots[0][0]},0")
                    want = bytes([0xAA, int(slots[0][1]) & 0x7F, 0x10, 0x00])
                else:
                    nc.test_action({"type": "serial", "port": port, "cmd": "U" * 16})
                    want = b"U" * 16 + b"\r"
                link.expect(want, timeout=3, since=m)
                link.verified = True
            except AssertionError as e:
                link.verified = False
                notes.append(f"{link.key}: wire found but its bytes did not verify at {bauds[port]} baud - {_line1(e)}")
            finally:
                try:
                    self.release(link)
                except Exception:  # noqa: BLE001
                    link.channel = link.params = None

        held = {(l.probe_name, l.header) for l in self.links.values()} | {(l.probe_name, l.header)
                                                                           for l in found.values()}
        for port, old in prior.items():
            if port in found or port in swept:
                continue
            if (old.probe_name, old.header) in held:
                notes.append(f"{old.key}: dropped the wire found before - {old.probe_name} {old.header} now belongs "
                             f"to another wire")
                continue
            found[port] = NcLink(self, port, old.probe_name, old.header, old.swap, tap=old.tap, verified=old.verified,
                                 node=old.wcb)
            held.add((old.probe_name, old.header))
            notes.append(f"{old.key}: kept the wire found before (its pin was not swept this time)")
        return found
