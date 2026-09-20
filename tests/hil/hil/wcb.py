"""WCB USB (S0) driver.

USB commands are drained one at a time from a single queue in loop() (WCB.ino:8241-8258), so
`?VERSION` sent right after a command is a reliable end-of-output sentinel: everything the
command printed synchronously lands before `End of Version` (WCB.ino:5777-5779).
"""
import re
import secrets
import time
import zlib
from contextlib import contextmanager

BOOT_LINE = r"^Booting up the Wireless Communication Board"


def chain_crc(chain):
    """WCB calculateCRC32 (WCB.ino:811-823) is standard reflected CRC-32 over the chain text."""
    return "%08X" % (zlib.crc32(chain.encode()) & 0xFFFFFFFF)


def group_tokens(tokens, lfi="?"):
    """Re-join a ?SEQ,SAVE value that contained '^'. Restore treats everything up to the next
    '^<LFI>' as the value (WCB.ino:2326-2349), so a naive '^' split breaks those apart."""
    out = []
    for t in tokens:
        if out and out[-1].upper().startswith(lfi + "SEQ,SAVE,") and not t.startswith(lfi):
            out[-1] += "^" + t
        else:
            out.append(t)
    return out


# Tokens that legitimately change without anyone touching the config.
VOLATILE_TOKENS = ("?PEERSLIVE,",)


def comparable(tokens):
    return [t for t in group_tokens(tokens) if not t.upper().startswith(VOLATILE_TOKENS)]


def split_checked_chain(text):
    """'<chain>^?CHK<8hex>' -> (tokens, provided, calculated)."""
    m = re.match(r"^(.*)\^[?]CHK([0-9A-Fa-f]{8})$", text)
    if not m:
        raise AssertionError(f"not a checksummed chain: {text[:120]}...")
    chain, provided = m.group(1), m.group(2).upper()
    return chain.split("^"), provided, chain_crc(chain)


class WCB:
    def __init__(self, dev):
        self.dev = dev

    # ------------------------------------------------------------ primitives
    def run(self, command, timeout=5.0):
        """Send one command, then a unique `;S0,HILEND<random>` echo; return the lines printed before it.

        The echo must be unique per call. A fixed sentinel (`?VERSION` -> `End of Version`) was
        satisfied by a stale `End of Version` from an EARLIER command that arrived just after the
        mark; every later read then parsed the previous command's output, and a test that relied
        on it never ran its own restore — that is how a reboot test once left a label and a stored
        sequence behind. (Tests that change ?CMDCHAR must not use run() until it is restored.)"""
        end = "HILEND" + secrets.token_hex(4).upper()
        m = self.dev.mark()
        self.dev.send(command)
        self.dev.send(f";S0,{end}")
        self.dev.expect(rf"^{end}$", timeout=timeout, since=m)
        lines = self.dev.since(m)
        return lines[:lines.index(end)]

    def send(self, command):
        m = self.dev.mark()
        self.dev.send(command)
        return m

    def rebooted_since(self, mark):
        return any(re.search(BOOT_LINE, t) for t in self.dev.since(mark))

    def wait_boot(self, since, timeout=20.0):
        """Wait for the last line of setup() (WCB.ino:8200), then past the USB reader's 500 ms
        start delay (WCB.ino:7270) — commands sent sooner are dropped by the boot flush."""
        self.dev.expect(r"^Raw Serial Forwarding Task Created", timeout=timeout, since=since)
        time.sleep(1.5)
        self.version()

    def reboot(self, timeout=20.0):
        m = self.dev.mark()
        self.dev.send("?reboot")
        self.dev.expect(r"^Rebooting in 2 seconds", timeout=3, since=m)
        self.wait_boot(m, timeout)
        return m

    # ------------------------------------------------------------ reads
    def version(self):
        m = self.dev.mark()
        self.dev.send("?VERSION")
        return self.dev.expect(r"^Software Version: (\S+)", timeout=3, since=m).group(1)

    def config_lines(self):
        return self.run("?config")

    def backup_chain(self):
        """The 'For Configured Boards' chain from ?backup, CRC-checked."""
        lines = self.run("?backup", timeout=8)
        for i, t in enumerate(lines):
            if "*** === For Configured Boards" in t:
                for nxt in lines[i + 1:]:
                    if re.search(r"\^.CHK[0-9A-Fa-f]{8}\s*$", nxt):
                        return split_checked_chain(nxt.strip())
        raise AssertionError(f"?backup had no 'For Configured Boards' chain ({len(lines)} lines read)")

    # The target ignores a CONFIG_REQ from the same requester within 1500 ms of the last one
    # it answered (handleConfigReqPacket, WCB.ino:3354) — meant to collapse the relay's 3-copy
    # burst, but it also swallows a genuine re-pull that soon, which then just times out.
    PULL_SPACING_S = 1.7

    def mgmt_pull(self, wcb, timeout=10.0):
        """?MGMT,PULL,<n> -> (version, tokens, provided_crc, calculated_crc)."""
        last = getattr(self.dev, "_last_pull", {}).get(wcb)
        if last is not None:
            wait = self.PULL_SPACING_S - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self.dev.__dict__.setdefault("_last_pull", {})[wcb] = time.monotonic()
        m = self.dev.mark()
        self.dev.send(f"?MGMT,PULL,{wcb}")
        line = self.dev.expect(rf"^\[MGMT:CONFIG,{wcb}\]\[VER:([^\]]*)\](.*)$", timeout=timeout, since=m)
        tokens, provided, calc = split_checked_chain(line.group(2))
        return line.group(1), tokens, provided, calc

    def etm_board_stats(self):
        """{wcb: {'sent','ackd','retries','failed','online'}} from ?STATS."""
        out = {}
        for t in self.run("?STATS"):
            m = re.match(r"^WCB(\d+)(?: \(special\))?: Sent: (\d+), ACKd: (\d+), Retries: (\d+), Failed: (\d+), (.*)$", t)
            if m and "Unguaranteed" not in t:
                out[int(m.group(1))] = {"sent": int(m.group(2)), "ackd": int(m.group(3)),
                                        "retries": int(m.group(4)), "failed": int(m.group(5)),
                                        "online": m.group(6).startswith("Online")}
        return out

    # ------------------------------------------------------------ RAM-only toggles
    def debug(self, kind, on):
        """?DEBUG[,<kind>],ON|OFF — RAM only, cleared by reboot (WCB.ino:4953-5000)."""
        verb = f"?DEBUG,{kind},{'ON' if on else 'OFF'}" if kind else f"?DEBUG,{'ON' if on else 'OFF'}"
        self.run(verb)

    @contextmanager
    def remote_terminal(self, wcb, self_id):
        """Mirror WCB<n>'s console here as [TERM:n] lines for the duration (RAM-only session)."""
        m = self.dev.mark()
        self.dev.send(f";W{wcb},?RTERM,START,{self_id}")
        self.dev.expect(rf"^\[TERM:{wcb}\]\[RTERM\] Session started", timeout=5, since=m)
        try:
            yield
        finally:
            self.dev.send(f";W{wcb},?RTERM,STOP")
            time.sleep(0.5)

    def remote_run(self, wcb, command, until, timeout=5.0):
        """Run a command on WCB<n> (inside remote_terminal) and wait for [TERM:n]<until>."""
        m = self.dev.mark()
        self.dev.send(f";W{wcb},{command}")
        self.dev.expect(rf"^\[TERM:{wcb}\]{until}", timeout=timeout, since=m)
        return [t[len(f"[TERM:{wcb}]"):] for t in self.dev.since(m) if t.startswith(f"[TERM:{wcb}]")]
