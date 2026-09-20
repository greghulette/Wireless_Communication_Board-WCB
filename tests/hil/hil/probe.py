"""Host side of the wcb_probe v2 USB protocol (see tests/hil/wcb_probe/wcb_probe.ino)."""
import re
import time

from .serialdev import ExpectTimeout

PROBE_BAUD = 921600
PROBE_MIN_VERSION = 2
HW_CHANNELS = ("A", "B")        # hardware UART1 / UART2
SW_CHANNELS = ("C", "D", "E")   # EspSoftwareSerial
HEADERS = ("S1", "S2", "S3", "S4", "S5")

# Probe headers whose pins EspSoftwareSerial refuses on a classic ESP32, so only a hardware channel
# (A/B) can serve them. Its GpioCapabilities::isValidPin drops GPIO 6-11 (flash), 20, and 16-17
# when PSRAM is present (PICO-V3-02 has PSRAM) — SoftwareSerial.h:51-55. S1 is TX 8 / RX 21 and S2
# is TX 20 / RX 7, so neither works in either orientation; S3-S5 (25/4, 14/27, 13/26) are fine.
HW_ONLY_HEADERS = ("S1", "S2")


class Probe:
    def __init__(self, dev):
        self.dev = dev
        self.bound = {}   # channel -> dict(header, baud, fmt, invert, swap, rx_only)
        self.version = None
        self.mesh_id = 0

    def _cmd(self, cmd, timeout=2.0):
        m = self.dev.mark()
        self.dev.send(cmd)
        got = self.dev.expect(r"^(OK|ERR)\b.*", timeout=timeout, since=m)
        if got.group(1) == "ERR":
            raise AssertionError(f"{self.dev.name}: '{cmd}' -> {got.group(0)}")
        return got.group(0)

    # ------------------------------------------------------------ identity / state
    def hello(self):
        m = self.dev.mark()
        self.dev.send("HELLO")
        got = self.dev.expect(r"^HELLO wcb_probe (\S+) mac=(\S+)(?: mesh=(\d+))?", timeout=3, since=m)
        self.version = got.group(1)
        self.mesh_id = int(got.group(3) or 0)
        return got

    def require_version(self):
        if self.version is None:
            self.hello()
        if not self.version.isdigit() or int(self.version) < PROBE_MIN_VERSION:
            raise AssertionError(f"{self.dev.name} runs wcb_probe v{self.version}; the harness needs "
                                 f"v{PROBE_MIN_VERSION}+ — flash tests/hil/wcb_probe")

    def reset(self):
        """Release every channel, pin, rule and PWM. The probe keeps state across host runs."""
        self.require_version()
        self._cmd("RESET")
        self.bound.clear()

    def scan(self):
        m = self.dev.mark()
        self.dev.send("SCAN")
        line = self.dev.expect(r"^SCAN ", timeout=3, since=m).string
        return {h: (tx, rx) for h, tx, rx in re.findall(r"(S\d) tx=(\S+) rx=(\S+)", line)}

    def clear(self):
        self._cmd("CLEAR")

    # ------------------------------------------------------------ serial channels
    def bind(self, ch, header, baud, fmt="8N1", invert=False, swap=False, rx_only=False):
        cmd = f"BIND {ch} {header} {baud} FMT={fmt}"
        cmd += " INV" if invert else ""
        cmd += " SWAP" if swap else ""
        cmd += " RXONLY" if rx_only else ""
        self._cmd(cmd)
        self.bound[ch] = dict(header=header, baud=baud, fmt=fmt, invert=invert, swap=swap, rx_only=rx_only)

    def unbind(self, ch):
        self._cmd(f"UNBIND {ch}")
        self.bound.pop(ch, None)

    def tx(self, ch, data: bytes):
        self._cmd(f"TX {ch} {data.hex().upper()}", timeout=5.0)

    def bursts(self, ch, since):
        """[(probe_ms, bytes)] received on `ch` after `since`."""
        out = []
        for text in self.dev.since(since):
            m = re.match(rf"^RX {ch} (\d+) ([0-9A-F]*)$", text)
            if m:
                out.append((int(m.group(1)), bytes.fromhex(m.group(2))))
        return out

    def received(self, ch, since):
        return b"".join(b for _, b in self.bursts(ch, since))

    def time_of(self, ch, marker: bytes, since):
        """Probe millis() of the burst holding the first byte of `marker`, or None."""
        stream, starts = b"", []
        for ms, chunk in self.bursts(ch, since):
            starts.append((len(stream), ms))
            stream += chunk
        i = stream.find(marker)
        if i < 0:
            return None
        return [ms for start, ms in starts if start <= i][-1]

    def errors(self, ch, since):
        return [t for t in self.dev.since(since) if t.startswith(f"RXERR {ch} ")]

    def expect_bytes(self, ch, expected: bytes, timeout=3.0, since=None):
        """Wait until the bytes received on `ch` since the mark contain `expected`."""
        start = self.dev.mark() if since is None else since
        deadline = time.monotonic() + timeout
        while True:
            got = self.received(ch, start)
            if expected in got:
                return got
            if time.monotonic() >= deadline:
                raise ExpectTimeout(f"{self.dev.name} ch {ch}: expected {expected!r} ({expected.hex(' ')}), "
                                    f"got {got!r} ({got.hex(' ')})")
            time.sleep(0.05)

    def expect_silence(self, ch, window=1.5, since=None):
        start = self.dev.mark() if since is None else since
        time.sleep(window)
        got = self.received(ch, start)
        if got:
            raise AssertionError(f"{self.dev.name} ch {ch}: expected nothing, got {got!r} ({got.hex(' ')})")

    # ------------------------------------------------------------ reply rules
    def rule_add(self, rule_id, ch, pattern, reply: bytes, delay_ms=0, once=False):
        """pattern: hex with '??' for any byte, e.g. 'AA0110??'. The probe answers on-device."""
        pat = pattern.replace(" ", "").upper()
        self._cmd(f"RULE ADD {rule_id} {ch} {pat} {reply.hex().upper()} DELAY={delay_ms}" + (" ONCE" if once else ""))

    def rule_del(self, rule_id):
        self._cmd(f"RULE DEL {rule_id}")

    def rule_clear(self):
        self._cmd("RULE CLEAR")

    def rule_hits(self, rule_id, since):
        return [int(m.group(1)) for m in (re.match(rf"^RULEHIT {rule_id} (\d+)", t) for t in self.dev.since(since)) if m]

    # ------------------------------------------------------------ edges / levels
    def edges_start(self):
        self._cmd("EDGES START")

    def edges_stop(self):
        self._cmd("EDGES STOP")

    def edges_read(self):
        """{'S1': {'tx': int|'busy'|'storm', 'rx': ...}, ...} — counts since the previous read."""
        m = self.dev.mark()
        self.dev.send("EDGES READ")
        line = self.dev.expect(r"^EDGES S1 ", timeout=3, since=m).string
        out = {}
        for h, tx, rx in re.findall(r"(S\d) tx=(\S+) rx=(\S+)", line):
            out[h] = {"tx": int(tx) if tx.isdigit() else tx, "rx": int(rx) if rx.isdigit() else rx}
        return out

    def level(self, header, which, value="READ"):
        if str(value).upper() == "READ":
            m = self.dev.mark()
            self.dev.send(f"LEVEL {header} {which} READ")
            return int(self.dev.expect(rf"^LEVEL {header} {which.upper()} ([01])", timeout=2, since=m).group(1))
        self._cmd(f"LEVEL {header} {which} {value}")

    # ------------------------------------------------------------ PWM
    def pwm_in(self, header, swap=False):
        self._cmd(f"PWMIN {header}" + (" SWAP" if swap else ""))

    def pwm_in_off(self, header=None):
        self._cmd("PWMIN OFF" + (f" {header}" if header else ""))

    def pwm_out(self, header, us, swap=False, hz=50):
        self._cmd(f"PWMOUT {header} {us} HZ={hz}" + (" SWAP" if swap else ""))

    def pwm_out_off(self, header=None):
        self._cmd("PWMOUT OFF" + (f" {header}" if header else ""))

    def pwm_pulses(self, since, header=None):
        """[(width_us, pulses_since_last_report)] from 'PWM S<h> <w> n=<k>' lines (NONE lines skipped)."""
        out = []
        for text in self.dev.since(since):
            m = re.match(r"^PWM (S\d) (\d+) n=(\d+)", text)
            if m and (header is None or m.group(1) == header):
                out.append((int(m.group(2)), int(m.group(3))))
        return out

    def pwm_readings(self, since, header=None):
        vals = []
        for text in self.dev.since(since):
            m = re.match(r"^PWM (S\d) (\d+|NONE)", text)
            if m and (header is None or m.group(1) == header):
                vals.append(None if m.group(2) == "NONE" else int(m.group(2)))
        return vals

    # ------------------------------------------------------------ mesh mode
    def mesh_join(self, device_id, oct2, oct3, password, quantity, channel=1, checksum=True,
                  temporary=True, dev_type="HILProbe"):
        if " " in password or " " in dev_type:
            raise AssertionError("mesh password / type may not contain spaces")
        self._cmd(f"MESH JOIN ID={device_id} OCT2={oct2:02X} OCT3={oct3:02X} QTY={quantity} CHAN={channel} "
                  f"CHK={int(checksum)} TEMP={int(temporary)} TYPE={dev_type} PASS={password}", timeout=10)
        self.mesh_id = device_id

    def mesh_leave(self, timeout=12.0):
        """Leaving means rebooting — WCB_Client has no end().

        Never anchor the BOOT line with ^: the ESP32 ROM and bootloader print their reset banner at 115200, which
        arrives at 921600 as NULs with no newline and lands on the front of 'BOOT wcb_probe ...'. The probe has
        already left once it answers, so local state is cleared before the wait: a timeout must not make
        probe_in_mesh send a second LEAVE."""
        m = self.dev.mark()
        self.dev.send("MESH LEAVE")
        self.bound.clear()
        self.mesh_id = 0
        self.dev.expect(r"BOOT wcb_probe \S+ mac=", timeout=timeout, since=m)

    def mesh_send(self, target, text, ensured=True):
        return self._cmd(f"MSEND {target} {int(ensured)} {text.encode().hex().upper()}").endswith("1")

    def mesh_broadcast(self, text, ensured=True):
        return self._cmd(f"MBROADCAST {int(ensured)} {text.encode().hex().upper()}").endswith("1")

    def mesh_raw(self, wcb, port, data: bytes):
        return self._cmd(f"MRAW {wcb} {port} {data.hex().upper()}").endswith("1")

    def mesh_received(self, since):
        """[(sender, text)] of commands the probe received over the mesh."""
        out = []
        for t in self.dev.since(since):
            m = re.match(r"^MRX (\d+) ([0-9A-F]*)$", t)
            if m:
                out.append((int(m.group(1)), bytes.fromhex(m.group(2)).decode("utf-8", errors="replace")))
        return out

    def mesh_state(self):
        m = self.dev.mark()
        self.dev.send("MESH STATE")
        got = self.dev.expect(r"^MSTATE id=(\d+) online=(\S+)", timeout=2, since=m)
        online = [] if got.group(2) == "-" else [int(x) for x in got.group(2).split(",")]
        return int(got.group(1)), online
