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

from .probe import HEADERS, HW_CHANNELS, HW_ONLY_HEADERS, SW_CHANNELS
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

    def bursts(self, since):
        return self.probe.bursts(self._ch(), since)

    def received(self, since):
        return self.probe.received(self._ch(), since)

    def time_of(self, marker: bytes, since):
        return self.probe.time_of(self._ch(), marker, since)

    def errors(self, since):
        return self.probe.errors(self._ch(), since)

    def expect(self, data: bytes, timeout=3.0, since=None):
        return self.probe.expect_bytes(self._ch(), data, timeout=timeout, since=since)

    def expect_silence(self, window=1.5, since=None):
        return self.probe.expect_silence(self._ch(), window=window, since=since)

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
        self.discovered_at = None

    # ------------------------------------------------------------ persistence
    def load(self):
        if not os.path.exists(self.path):
            return False
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
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
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"discovered_at": self.discovered_at,
                       "links": [l.to_json() for l in self.links.values()]}, f, indent=2)

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
        for link in self.links.values():
            if link.probe_name == probe_name:
                link.channel = None
                link.params = None

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
        """Make the WCB port transmit and check the bytes; on silence, try the other orientation."""
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
        probe.bind(ch, link.header, baud, fmt=fmt, invert=invert, swap=link.swap, rx_only=link.tap)
        for other in self.links.values():   # belt and braces: one channel, one link
            if other is not link and other.probe_name == link.probe_name and other.channel == ch:
                other.channel, other.params = None, None
        owned[ch] = link
        link.channel, link.params, link.last_used = ch, params, time.monotonic()

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
