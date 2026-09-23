"""Wires between probe headers and WCB ports: discovery, persistence, and channel allocation.

Nobody hand-maintains the wiring map. `discover()` makes each WCB port transmit in turn while
every probe counts edges on every header pin; the pin that toggles is the wire, and whether it
landed on the header's TX or RX pin says whether the cable is crossed or straight-through. The
result is saved to results/links.json and reused until the next `--discover`.

A probe has five channels: A/B hardware UARTs and C/D/E software serial. A link is bound to a
channel only while a test uses it; hardware channels go to links above SW_MAX_BAUD, and when a
probe runs out the least-recently-used link on it gives its channel up.
"""
import json
import os
import re
import time
from datetime import datetime

from .checkpoint import atomic_write_text
from .probe import HEADERS, HW_CHANNELS, HW_ONLY_HEADERS, SW_CHANNELS, restart_what
from .wcb import WCB

SW_MAX_BAUD = 38400


class Link:
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
            return False
        self.links = {}
        for d in data.get("links", []):
            if d["probe"] in self.bench.cfg["devices"]:
                link = Link(self, **d)
                if self.device_on(link.wcb, link.port) is not None:
                    link.tap = True   # a links.json older than the device must not stay transmit-capable
                self.links[(link.wcb, link.port)] = link
        self.discovered_at = data.get("discovered_at")
        return True

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Atomic: a power-off mid-write used to leave a truncated links.json, and Bench() then refused to load.
        atomic_write_text(self.path, json.dumps({"discovered_at": self.discovered_at,
                                                 "links": [l.to_json() for l in self.links.values()]}, indent=2))

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
        nothing may be sent there — even when a listen-only wire is on it; raw=True (GUI, plan) returns it anyway."""
        link = self.links.get((wcb, port))
        if link is not None and not raw and self.device_only(wcb, port):
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
            raise Skip(f"no wire on W{wcb} {port} (run with --discover after wiring)")
        return link

    def all(self, duplex_only=False):
        return [l for l in self.links.values() if not (duplex_only and l.tap)]

    # ------------------------------------------------------------ real devices on WCB ports
    def device_on(self, wcb, port):
        """The real device bench.json "port_devices" says is wired to this WCB port, or None."""
        return self.bench.cfg.get("port_devices", {}).get(f"W{wcb}{port}")

    def device_only(self, wcb, port):
        """A real device is on the port and no port_stimulus names a safe way to make the port transmit, so the
        tool must not make it transmit at all (discovery, verify and link.out skip it)."""
        return self.device_on(wcb, port) is not None and f"W{wcb}{port}" not in self.bench.cfg.get("port_stimulus", {})

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
        for link in self.links.values():
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
        stale = [(l, self.rebooted_since_bind(l)) for l in self.links.values()
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
        link = Link(self, wcb, port, probe, header, swap=swap, tap=tap)
        self.links[(wcb, port)] = link
        self.save()
        return link

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
                WCB(self.bench.dev(console)).send(cmd)
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
                WCB(self.bench.dev(console)).send(cmd)
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
            baud = self.bench.port_baud(link.wcb, link.port)
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
        for other in self.links.values():   # belt and braces: one channel, one link
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
        as ;W<n>;S over the mesh. Ports with a real device use bench.json's port_stimulus."""
        console = self.console_for(wcb)
        override = self.bench.cfg.get("port_stimulus", {}).get(f"W{wcb}{port}")
        if override:
            return console, override["send"], bytes.fromhex(override["expect"])
        payload = text or "U" * 16   # 0x55: an edge on every bit
        n = port[1]
        local = console != "wcb1" or wcb == self.bench.usb_wcb_number()
        cmd = f";S{n}{payload}" if local else f";W{wcb};S{n}{payload}"
        return console, cmd, payload.encode() + b"\r"

    def discover(self, log=print):
        bench = self.bench
        probes = {n: bench.probe(n) for n in bench.probe_names()}
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
        found, notes = {}, []
        try:
            for w, port in ports:
                if f"W{w}{port}" in skip or self.device_only(w, port):
                    if self.device_only(w, port):
                        notes.append(f"W{w}{port}: skipped — {self.device_on(w, port).get('kind', 'a device')} on it and no port_stimulus")
                    continue
                console, cmd, _ = self.stimulus(w, port)
                time.sleep(0.1)
                for p in probes.values():
                    p.edges_read()
                time.sleep(0.3)
                base = {n: p.edges_read() for n, p in probes.items()}
                WCB(bench.dev(console)).send(cmd)
                over_mesh = console == "wcb1" and w != bench.usb_wcb_number()
                time.sleep(0.9 if over_mesh else 0.45)
                hit = {n: p.edges_read() for n, p in probes.items()}
                cands = []
                for n in probes:
                    for h in HEADERS:
                        for which in ("tx", "rx"):
                            v, b = hit[n][h][which], base[n][h][which]
                            if isinstance(v, int) and isinstance(b, int) and v >= 6 and b == 0:
                                cands.append((v, n, h, which))
                            elif b == "storm" or (isinstance(b, int) and b > 0):
                                notes.append(f"{n} {h}.{which} noisy (baseline {b})")
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

        self.links = found
        for link in self.links.values():
            console, cmd, expected = self.stimulus(link.wcb, link.port)
            try:
                m = link.mark()
                WCB(bench.dev(console)).send(cmd)
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
        self.discovered_at = datetime.now().isoformat(timespec="seconds")
        self.save()
        for note in notes:
            log(f"  note: {note}")
        return list(self.links.values())
