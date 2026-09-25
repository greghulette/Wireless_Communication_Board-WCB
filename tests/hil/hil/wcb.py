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
        # The tail, not the head: the head is where ?WIFI and ?EPASS sit (collectConfigCommands), and the tail is what
        # shows why ^?CHK is not last - e.g. an interleaved "[ETM] WCBn came ONLINE".
        raise AssertionError(f"not a checksummed chain ({len(text)} chars): ...{text[-80:]}")
    chain, provided = m.group(1), m.group(2).upper()
    return chain.split("^"), provided, chain_crc(chain)


# ------------------------------------------------------------------ the mesh config pull (?MGMT,PULL)
# A relay prints one console line per message of a config pull, and picks the tag from the ESP-NOW packet type, never
# from the body (HIL_TEST_AUDIT.md F13):
#   [MGMT:CONFIG,<n>]<reply>                  a reply of PULL_MAX characters or less, exactly as before F13: one relay
#                                             session holds MGMT_MAX_CHUNKS x 182
#   [MGMT:CFGPART,<n>]P<id>,<k>,<K>:<data>~   part k of K of a longer reply. The data of parts 1..K of one id, in order,
#                                             IS the reply. Parts split it by byte offset, not at tokens, so they are
#                                             joined as text before any '^' split: a ?SEQ,SAVE value may hold '^' and
#                                             even '^?'. '~' closes the data, so trailing spaces survive.
#   [MGMT:CFGERR,<n>]<CODE>,<detail>          the target refused, and says why without any config text
# <reply> is "[VER:<fw>]<chain>^?CHK<8 hex>": the target's factory chain behind its version, CRC-32 over the chain.
# ",P" asks for parts. Without it an over-limit config gets CFGERR NOPARTS, never a part, because an old Wizard stores
# ANY non-empty [MGMT:CONFIG,n] body as the board's config, unchecked. A relay built before F13 reads "<n>,P" as <n>
# (toInt / atoi) and only ever prints the CONFIG line, so the same call still works through it under the limit.
PULL_MAX = 16 * 182
PULL_TAG = re.compile(r"^\[MGMT:(CONFIG|CFGPART|CFGERR),(\d+)\](.*)$", re.S)
PART_BODY = re.compile(r"P([0-9A-F]{4}),(\d{1,2}),(\d{1,2}):(.*)~", re.S)     # fullmatch: the '~' must end the line
# Every code a target's CFGERR carries (cpjFail, WCB.ino), and EMPTY for the bare legacy reply: out of memory on a
# target, seen alone through a relay with no CFGERR.
REFUSALS = ("NOMEM", "CHANGED", "NOPARTS", "TOOBIG", "EMPTY")
# read_config's retry policy, and nothing else's: a test that judges an answer names the codes it accepts, because
# what may pass differs by test (a ,P pull through a working relay never ends in NOPARTS, yet read_config retries it).
# NOMEM (the target could not allocate the reply buffer) and CHANGED (the config changed under the build, three times
# running) pass on their own, and so does NOPARTS on a ,P pull (every type-19 copy of the request was lost while a
# type-5 copy got through; Pull already asks once more itself); TOOBIG (more than 16 parts) comes back every time, and
# so does MALFORMED (see REFUSAL_CODE).
RETRYABLE = ("NOMEM", "CHANGED", "NOPARTS", "EMPTY")
# What config text looks like. A CFGERR detail never carries any; one that does is a firmware bug, and its detail is
# withheld rather than quoted, since the first 120 characters of a chain hold ?WIFI and ?EPASS.
CONFIG_TEXT = re.compile(r"\^|\[VER:|\?HW,|EPASS|WIFI,|SEQ,SAVE")
# A code as the firmware writes one. The code is everything before a CFGERR body's first comma, so config text under
# the tag - the bug CONFIG_TEXT is there to catch - puts a token or a bare secret in it, where CONFIG_TEXT cannot see
# it: '?EPASS,hunter2' parses as code '?EPASS', detail 'hunter2'. Any other code is quoted as MALFORMED.
REFUSAL_CODE = re.compile(r"[A-Z]{2,12}")

# The target ignores a config request from a requester it ACCEPTED one from under 1500 ms ago, and one from the
# requester whose reply it is still sending (handleConfigReqPacket, WCB.ino). That collapses the relay's 3-copy burst,
# but it also swallows a genuine re-pull that soon, which then just times out. pull_config stamps the time the reply
# completed - never earlier than the target's acceptance - so waiting 1.7 s from it always clears the window.
PULL_SPACING_S = 1.7


def screen_code(code):
    """A CFGERR code fit to quote in a message or a note: as the firmware writes one, or else 'MALFORMED'."""
    return code if REFUSAL_CODE.fullmatch(code) else "MALFORMED"


class PullRefused(AssertionError):
    """The target answered a config pull with a reason instead of a config: a [MGMT:CFGERR,<n>] line, or the bare
    [MGMT:CONFIG,<n>] (code EMPTY). An AssertionError, so every caller that treats a failed read as a failure still
    does; read_config retries it only when `retryable`. A line that does not start with a code is code MALFORMED,
    permanent."""

    def __init__(self, wcb, code, detail=""):
        # The code and the detail go into messages and notes, so both are screened: the code must look like the
        # firmware's, and the detail - the target's own ASCII (sizes, a hint) - is quoted only beside a code the
        # firmware sends, cut to 120 characters, and withheld outright if it looks like config text. Anything else
        # means config text may have reached the tag (a relay or firmware bug): counted, never quoted.
        detail = "".join(c if " " <= c <= "~" else "?" for c in detail)
        why, held = None, len(detail)
        if screen_code(code) != code:
            why, held, code = "the line does not start with a refusal code", len(code) + len(detail), "MALFORMED"
        elif code not in REFUSALS:
            why = f"{code} is not a code the firmware sends"
        elif CONFIG_TEXT.search(detail):
            why = "they look like config text"
        self.wcb, self.code, self.retryable = wcb, code, code in RETRYABLE
        self.mark = None                        # set by Pull.poll: the relay console's mark at the send
        self.detail = detail[:120] if why is None else (f"<{held} characters withheld: {why}>" if held else "")
        what = ("an empty config reply (out of memory on the target?)" if code == "EMPTY"
                else f"CFGERR {code}" + (f" ({self.detail})" if self.detail else ""))
        super().__init__(f"W{wcb} refused the config pull: {what} - {'retryable' if self.retryable else 'permanent'}")


def parse_pull_line(line):
    """'[MGMT:<TAG>,<n>]<body>' -> (tag, n, body), or None for any other line."""
    m = PULL_TAG.match(line)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else None


def parse_part(body):
    """A CFGPART body -> (id, k, K, data), or None unless it is strictly 'P<4 hex>,<k>,<K>:<data>~' with 1<=k<=K<=16."""
    m = PART_BODY.fullmatch(body)
    if not m:
        return None
    k, total = int(m.group(2)), int(m.group(3))
    if not 1 <= k <= total <= 16:
        return None
    return m.group(1), k, total, m.group(4)


def parse_error(body):
    """A CFGERR body '<CODE>,<detail>' -> (code, detail)."""
    code, _, detail = body.partition(",")
    return code.strip(), detail.strip()


class PullReply:
    """One complete answer to a config pull. `text` is the whole reply and `parts` the data of each part: both carry
    ?EPASS and ?WIFI, so neither may be printed, noted or quoted in an assertion. __repr__ and every message raised here
    give lengths only - unlike split_checked_chain, whose tail quote is fine for ?backup but not for a short reply."""

    def __init__(self, wcb, kind, text, mark, part_id=None, parts=()):
        self.wcb, self.kind, self.text, self.mark = wcb, kind, text, mark
        self.part_id, self.parts = part_id, list(parts)
        self.part_lengths = [len(p) for p in self.parts]
        self.resent = []                        # set by Pull.reply: the refusals it asked again after (Pull)
        m = re.match(r"^\[VER:([^\]]*)\](.*)$", text, re.S)
        if not m:
            raise AssertionError(f"W{wcb}: the {kind} reply ({len(text)} chars) does not start with [VER:<fw>]")
        self.version = m.group(1)
        c = re.match(r"^(.*)\^[?]CHK([0-9A-Fa-f]{8})$", m.group(2), re.S)
        if not c:
            raise AssertionError(f"W{wcb}: the {kind} reply ({len(text)} chars, parts {self.part_lengths}) does not "
                                 f"end in ^?CHK<8 hex> (its last '^?CHK' is at {text.rfind('^?CHK')})")
        self.tokens, self.provided, self.calc = c.group(1).split("^"), c.group(2).upper(), chain_crc(c.group(1))

    @property
    def count(self):
        return len(self.parts) if self.kind == "parts" else 1

    def __repr__(self):
        return f"PullReply(W{self.wcb}, {self.kind}, {len(self.text)} chars, parts {self.part_lengths})"


class PullCollector:
    """The reply to one pull, a console line at a time: the harness twin of the Wizard's createPullCollector
    (Wizard/parser.js). feed() returns 'ignored' (another board, or not a pull line), 'malformed', 'duplicate' (a part
    already held: no progress), 'partial' (a new part) or 'complete' (then .reply is set), and raises PullRefused for a
    CFGERR or an empty legacy line. A part of a new id (or a new part count) drops the parts held so far: the target
    restarts a reply under a new id when its config changed mid-build, and parts of two builds never join into one."""

    def __init__(self, wcb, mark=0):
        self.wcb, self.mark = wcb, mark
        self.part_id, self.total, self.parts = None, None, {}
        self.reply = None
        self.malformed = self.restarts = self.others = 0

    def feed(self, line):
        got = parse_pull_line(line)
        if not got:
            return "ignored"
        tag, n, body = got
        if n != self.wcb:
            self.others += 1
            return "ignored"
        if tag == "CFGERR":
            raise PullRefused(self.wcb, *parse_error(body))
        if tag == "CONFIG":
            if not body.strip():
                raise PullRefused(self.wcb, "EMPTY")
            self.reply = ("legacy", body, None, [])
            return "complete"
        part = parse_part(body)
        if part is None:
            self.malformed += 1
            return "malformed"
        pid, k, total, data = part
        if (pid, total) != (self.part_id, self.total):
            if self.part_id is not None:
                self.restarts += 1
            self.part_id, self.total, self.parts = pid, total, {}
        if k in self.parts:
            return "duplicate"
        self.parts[k] = data
        if len(self.parts) < self.total:
            return "partial"
        ordered = [self.parts[j] for j in range(1, self.total + 1)]
        self.reply = ("parts", "".join(ordered), pid, ordered)
        return "complete"

    def progress(self):
        """What arrived, as counts and part numbers only: safe for an assertion message."""
        have = (f"no reply line for W{self.wcb}" if self.part_id is None
                else f"parts {sorted(self.parts)} of {self.total} under id {self.part_id}")
        extra = [f"{v} {what}" for v, what in ((self.restarts, "id restarts"), (self.malformed, "malformed part lines"),
                                               (self.others, "pull lines for other boards")) if v]
        return have + (f" ({', '.join(extra)})" if extra else "")

    def result(self):
        kind, text, pid, parts = self.reply
        return PullReply(self.wcb, kind, text, self.mark, pid, parts)


def dev_note(dev, text):
    """A '#' line in session.log under the device's name, for what a helper decided on its own (SerialDevice.log)."""
    log = getattr(dev, "log", None)
    if log:
        log(getattr(dev, "name", "?"), "#", text)


class Pull:
    """One config pull in flight on a relay's console - wcb1, or NaviCore's WCB_Client relay. The constructor waits out
    PULL_SPACING_S and sends ?MGMT,PULL,<wcb>[,P]; poll() reads what has arrived since and returns True once the reply
    is whole, so a test can do other things while the target sends (pull_config just polls). `timeout` runs from the
    send and restarts on every new part: how many parts are coming is known only once one arrives, and a K-part reply
    takes about K x 0.7 s. Everything raised here carries lengths, counts and codes only, never a line: the reply holds
    the mesh password and the WiFi passphrase.

    A ,P pull answered CFGERR NOPARTS is sent once more, PULL_SPACING_S after the refusal, and session.log notes it. A
    relay sends the parts request (type 19) three times and then the plain one (type 5) three times, all broadcast,
    and a board often drops the first frame it is sent, so every type-19 copy can be lost while a type-5 copy gets
    through: the target then refuses the parts it was never asked for. Only a second NOPARTS is raised - a relay that
    never sends type 19 gets one every time. poll() waits for the re-send without blocking, `resent` names what it
    re-sent after, and `mark` stays at the first send, so a scan for stray lines since it covers both. A test that
    wants the first answer as it came (one expecting a refusal) passes retry_noparts=False."""

    def __init__(self, dev, wcb, parts=True, timeout=10.0, retry_noparts=True):
        self.dev, self.wcb, self.timeout = dev, wcb, timeout
        self.retry_noparts = parts and retry_noparts
        self.stamps = dev.__dict__.setdefault("_last_pull", {})
        self.cmd = f"?MGMT,PULL,{wcb}" + (",P" if parts else "")
        self.mark, self.resent, self._resend_at = None, [], None
        self._send()

    def _send(self):
        last = self.stamps.get(self.wcb)
        if last is not None:
            wait = PULL_SPACING_S - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._i = self.dev.mark()
        if self.mark is None:
            self.mark = self._i
        self.stamps[self.wcb] = time.monotonic()     # a floor, for a pull that never completes
        self.dev.send(self.cmd)
        self.collector = PullCollector(self.wcb, self.mark)
        self.deadline = time.monotonic() + self.timeout

    def poll(self):
        """True once the reply is whole; raises PullRefused (with .mark) or AssertionError when the deadline passes."""
        if self.collector.reply is not None:
            return True
        if self._resend_at is not None:
            if time.monotonic() < self._resend_at:
                return False
            self._resend_at = None
            self._send()                        # lines since the refusal are skipped: a new collector and deadline
        col = self.collector
        for line in self.dev.since(self._i):
            self._i += 1
            try:
                ev = col.feed(line)
            except PullRefused as e:
                self.stamps[self.wcb] = time.monotonic()
                if e.code == "NOPARTS" and self.retry_noparts and not self.resent:
                    self.resent.append(e.code)
                    self._resend_at = self.stamps[self.wcb] + PULL_SPACING_S
                    dev_note(self.dev, f"{self.cmd} was answered CFGERR NOPARTS: every parts request (type 19) lost? "
                                       f"sending it once more")
                    return False
                e.mark = self.mark
                raise
            if ev == "partial":
                self.deadline = time.monotonic() + self.timeout
            elif ev == "complete":
                self.stamps[self.wcb] = time.monotonic()
                return True
        if time.monotonic() > self.deadline:
            raise AssertionError(f"{self.dev.name}: no complete reply to {self.cmd} within {self.timeout:.0f}s of the "
                                 f"last progress - {col.progress()}")
        return False

    def reply(self, verify=True):
        """The whole PullReply. verify=True raises AssertionError on a CRC mismatch (read_config retries that)."""
        r = self.collector.result()
        r.resent = list(self.resent)
        if verify and r.provided != r.calc:
            raise AssertionError(f"W{self.wcb}: the pulled config ({len(r.text)} chars as {r.kind}, parts "
                                 f"{r.part_lengths}) fails its CRC: CHK {r.provided}, calculated {r.calc}")
        return r


def pull_config(dev, wcb, parts=True, timeout=10.0, verify=True):
    """?MGMT,PULL,<wcb>[,P] on `dev`, waited out -> PullReply (see Pull: a ,P pull refused NOPARTS goes once more)."""
    p = Pull(dev, wcb, parts=parts, timeout=timeout)
    while not p.poll():
        time.sleep(0.05)
    return p.reply(verify=verify)


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
        # Drop relayed telemetry. While a host holds W1's RC relay window open (any ;W20,{json} does, for
        # 20 s), NaviCore's rc_ch/rc_hb/rc_trig arrive on W1's USB at up to 5 Hz, tagged {"sys":1,...}, and
        # land in the middle of whatever command is running - ?KYBER,LIST once came back with an rc_ch line
        # as its first line (maestro.list_and_legacy_spellings, 2026-09-22). They are never a command's own
        # output; tests that want them read self.dev directly.
        return [x for x in lines[:lines.index(end)] if not x.startswith('{"sys":1')]

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

    # How long a ?reboot may take to restart. Every command-queue item that does something restarts the 4 s quiet
    # window, printed or not; NaviCore's store-only ?STATS,RPT and a re-arm of a running ?RTERM session do not
    # (quietWindowExempt, WCB.ino; tracker #93). Before that, a STATS,RPT and two re-arms relayed from a WiFi client on
    # NaviCore failed etm.w1_reboot_sees_peers at 10 s (20260924-190733). Whatever keeps arriving, the firmware restarts
    # RESTART_MAX_DEFER_MS (20 s) after the request, with a 'Restart held off' line; 25 s leaves room to see that.
    REBOOT_DEFER_S = 25.0

    def reboot(self, timeout=20.0):
        m = self.dev.mark()
        t0 = time.monotonic()
        self.dev.send("?reboot")
        # ?reboot is deferred now (CLAUDE.md rule 11): the board acknowledges, then restarts once
        # the command queue has been quiet for PWM_REBOOT_QUIET_MS.
        self.dev.expect(r"^Reboot queued", timeout=3, since=m)
        self.dev.expect(r"^Rebooting now", timeout=self.REBOOT_DEFER_S, since=m)
        # Every time, so a restart creeping toward the limit shows in session.log before it fails a test.
        capped = any(t.startswith("Restart held off") for t in self.dev.since(m))
        dev_note(self.dev, f"?reboot restarted {time.monotonic() - t0:.1f} s after it was sent "
                           f"(quiet window 4 s, cap 20 s{' - REACHED' if capped else ''}, limit {self.REBOOT_DEFER_S:.0f} s)")
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

    def mgmt_pull(self, wcb, timeout=10.0):
        """?MGMT,PULL,<n>,P -> (version, tokens, provided_crc, calculated_crc), from the one-line reply or from its
        parts. Raises PullRefused for a CFGERR or an empty reply, AssertionError for a timeout or a CRC mismatch."""
        r = pull_config(self.dev, wcb, parts=True, timeout=timeout)
        return r.version, r.tokens, r.provided, r.calc

    def pull_reply(self, wcb, parts=True, timeout=10.0, verify=True):
        """The whole PullReply of ?MGMT,PULL,<n>[,P]: its kind, parts and the mark it was sent at. Never print its
        text."""
        return pull_config(self.dev, wcb, parts=parts, timeout=timeout, verify=verify)

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
