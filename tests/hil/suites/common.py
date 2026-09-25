"""Shared helpers for the suites."""
import re
import secrets
import time
from contextlib import contextmanager

from hil.config import read_config, token  # noqa: F401 — token re-exported for suites
from hil.wcb import WCB

# Console lines that arrive on their own (ETM/WDP/peer events, relayed terminals, JSON telemetry).
NOISE = re.compile(r"^(\[ETM\]|\[WDP|\[PEER\]|\[TERM:|\{|\[RCBRG\]|\[MGMT|\[WCB_Client\])")


def nonce():
    return secrets.token_hex(3).upper()


def marker(tag=""):
    """A unique ASCII marker, so bytes from an earlier test can never satisfy this one."""
    return f"HIL{tag}{nonce()}"


def padded(tag, length):
    """A unique marker padded with [0-9A-Z] to exactly `length` chars — never ; ^ * ? { or @."""
    return (marker(tag) + "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 20)[:length]


def prime(*links):
    """Send a bare CR into each port, so a fragment left by an earlier test can't prefix this one's
    first line (every port keeps an unterminated line in a static buffer, WCB.ino:7031-7068)."""
    for l in links:
        if l is not None and not l.tap:
            l.send(b"\r")


def send_chunked(l, data: bytes, chunk=1000):
    """Inject a long byte string. The probe's TX verb decodes into a 1024-byte buffer (wcb_probe/probe_main.cpp:654)
    and answers a longer chunk with a misleading 'ERR bad hex', so send at most 1024 bytes per TX."""
    for i in range(0, len(data), chunk):
        l.send(data[i:i + chunk])


def require_tokens(bench, wcb, *tokens):
    """Skip unless W<wcb>'s saved config contains every token (case-insensitive)."""
    have = {t.upper() for t in bench.config_tokens(wcb, refresh=True)}
    missing = [t for t in tokens if t.upper() not in have]
    if missing:
        from hil.runner import Skip
        raise Skip(f"W{wcb} config lacks {missing}")


def quiet_lines(dev, since):
    """Console lines after `since`, minus the unsolicited ones in NOISE."""
    return [t for t in dev.since(since) if t.strip() and not NOISE.search(t)]


class Watch:
    """Mark several wires at once, so a test can check what each received after one stimulus."""

    def __init__(self, *links):
        self.links = [l for l in links if l is not None]
        self.marks = {l.key: l.mark() for l in self.links}

    def got(self, l):
        return l.received(self.marks[l.key])

    def expect(self, l, data: bytes, timeout=3.0):
        return l.expect(data, timeout=timeout, since=self.marks[l.key])

    def silent(self, *links, window=1.5):
        time.sleep(window)
        noisy = [f"{l.key}: {self.got(l)!r}" for l in links if l is not None and self.got(l)]
        assert not noisy, "expected silence, got " + "; ".join(noisy)


class Console:
    """Read and drive W<wcb>'s console: its own USB cable if it has one, otherwise a ?RTERM session
    relayed to wcb1 (lines arrive as [TERM:n]...). Use as a context manager."""

    def __init__(self, bench, wcb):
        self.bench, self.wcb = bench, wcb
        self.prefix, self.rterm, self.remote = "", False, False

    def __enter__(self):
        b = self.bench
        own = "wcb1" if self.wcb == b.usb_wcb_number() else b.usb_wcbs().get(self.wcb)
        if own:
            try:
                self.dev = b.dev(own)
                return self
            except Exception:
                pass
        self.remote = True
        self.dev = b.dev("wcb1")
        self.prefix = f"[TERM:{self.wcb}]"
        m = self.dev.mark()
        self.dev.send(f";W{self.wcb},?RTERM,START,{b.usb_wcb_number()}")
        self.dev.expect(rf"^\[TERM:{self.wcb}\]\[RTERM\] Session started", timeout=5, since=m)
        self.rterm = True
        time.sleep(0.3)
        return self

    def __exit__(self, *exc):
        if self.rterm:
            self.dev.send(f";W{self.wcb},?RTERM,STOP")
            time.sleep(0.5)

    def mark(self):
        return self.dev.mark()

    def send(self, cmd):
        """One command (no ^ chains when remote: ;W splits them). Returns the mark taken before it."""
        m = self.dev.mark()
        self.dev.send(f";W{self.wcb},{cmd}" if self.remote else cmd)
        return m

    def expect(self, pattern, timeout=4.0, since=None):
        return self.dev.expect("^" + re.escape(self.prefix) + pattern.lstrip("^"), timeout=timeout, since=since)

    def lines(self, since):
        n = len(self.prefix)
        return [t[n:] for t in self.dev.since(since) if t.startswith(self.prefix)] if self.remote else self.dev.since(since)


def usb_wcb(bench):
    return WCB(bench.dev("wcb1"))


def usb_wcb_number(bench):
    return bench.usb_wcb_number()


def remote_wcbs(bench):
    """Every WCB other than the primary console — reachable over the mesh even if it also has USB."""
    return [w for w in bench.wcb_numbers() if w != bench.usb_wcb_number()]


# ------------------------------------------------------------------ wires
def link(bench, wcb, port):
    """The wire on W<wcb> <port> (a hil.links.Link), or Skip if none is connected."""
    return bench.links.require(wcb, port)


def wire(bench, wcb, port, baud=None, **kw):
    """Bind the wire on W<wcb> <port> and return (probe, channel). baud=None follows the WCB config."""
    l = link(bench, wcb, port)
    l.listen(baud, **kw)
    return l.probe, l.channel


def port_cmd(bench, wcb, port, text):
    """The console command that makes W<wcb> write `text` out of <port> (;S locally, ;W<n>;S remotely)."""
    n = port[1] if isinstance(port, str) else port
    return f";S{n}{text}" if wcb == bench.usb_wcb_number() else f";W{wcb};S{n}{text}"


# ------------------------------------------------------------------ config snapshots
def snapshot(bench, wcb):
    """Comparable config tokens for a WCB: ?backup for the USB board, ?MGMT,PULL for the rest."""
    return read_config(bench, wcb)


# Token kinds config_guard puts back on its own when a test leaks them (docs/HIL_TEST_AUDIT.md A1). Only settings a
# replayed backup line restores in place, with no reboot and no mesh side effect. Identity and radio (?WCB, ?WCBCH,
# ?MAC, ?EPASS), ETM, WDP, the controller, devices (?MAESTRO, ?KYBER, ?HCR, ?MP3, ?DFP, ?WLED) and ?MAP,PWM (which
# reboots) are left to the test and reported as not put back.
AUTO_RESTORE = ("?BAUD,", "?BCAST,", "?LABEL,", "?SEQ,SAVE,", "?MAP,SERIAL,", "?VAR,SET,", "?ALIAS,")
# A leaked one of these changes how commands are read: nothing can be sent safely until the test's own restore runs.
NO_AUTO_RESTORE_WHILE = ("?DELIM,", "?CMDCHAR,", "?FUNCCHAR,")


def _undo(leaked, before):
    """The command that removes a leaked token, or None when replaying the baseline's own token replaces it.

    A same-key ?SEQ,SAVE is overwritten in place (a clear-and-save would move the key to the end of the inventory and
    change the backup order); a same-port ?MAP,SERIAL replaces its destination list; ?BAUD / ?BCAST are values the
    baseline token sets back."""
    u = leaked.upper()
    m = re.match(r"^\?LABEL,(S\d),", u)
    if m:
        return None if any(t.upper().startswith(f"?LABEL,{m.group(1)},") for t in before) else f"?LABEL,CLEAR,{m.group(1)}"
    m = re.match(r"^\?SEQ,SAVE,([^,]+),", leaked, re.I)
    if m:
        return None if any(t.startswith(f"?SEQ,SAVE,{m.group(1)},") for t in before) else f"?SEQ,CLEAR,{m.group(1)}"
    m = re.match(r"^\?MAP,SERIAL,(S\d)", u)
    if m:
        return None if any(re.match(rf"^\?MAP,SERIAL,{m.group(1)}\b", t, re.I) for t in before) else f"?MAP,SERIAL,CLEAR,{m.group(1)}"
    m = re.match(r"^\?VAR,SET,([^,]+),", leaked, re.I)
    if m:
        return None if any(t.startswith(f"?VAR,SET,{m.group(1)},") for t in before) else f"?VAR,CLEAR,{m.group(1)}"
    if u.startswith("?ALIAS,"):
        return None if any(t.upper().startswith("?ALIAS,") for t in before) else "?ALIAS,CLEAR"
    return None


def _auto_restore(bench, wcb, before, after):
    """Send what puts W<wcb> back for the AUTO_RESTORE token kinds -> (commands sent, tokens left to the test).
    Undo lines go first (a leaked mapping's CLEAR also restores that port's broadcast flags), then the missing
    baseline tokens in backup order, as a restore would send them."""
    missing = [t for t in before if t not in after]
    extra = [t for t in after if t not in before]
    if any(t.upper().startswith(NO_AUTO_RESTORE_WHILE) for t in missing + extra):
        return [], missing + extra
    cmds, left = [], []
    for t in extra:
        if t.upper().startswith(AUTO_RESTORE):
            undo = _undo(t, before)
            if undo:
                cmds.append(undo)
        else:
            left.append(t)
    for t in missing:
        (cmds if t.upper().startswith(AUTO_RESTORE) else left).append(t)
    own = bench.usb_wcbs().get(wcb) or ("wcb1" if wcb == bench.usb_wcb_number() else None)
    sent = []
    for c in cmds:
        if own:
            WCB(bench.dev(own)).run(c, timeout=8)
        elif "^" in c:
            left.append(c)          # ;W<n>,a^b is split by the sender (WCB.ino): a ^ value cannot be replayed this way
            continue
        else:
            WCB(bench.dev("wcb1")).send(f";W{wcb},{c}")
            time.sleep(0.4)
        sent.append(c)
    if sent and not own:
        time.sleep(1.0)
    return sent, left


@contextmanager
def config_guard(bench, *wcbs):
    """Snapshot the config of each WCB, run the body, and fail if any board did not end up
    byte-identical. A leak is reported even when the body itself failed. Before the report, the
    guard puts back what it safely can (AUTO_RESTORE): the failure then says the bench is clean,
    or which tokens it left alone. Afterwards the config cache is refreshed and bound wires
    re-follow the (restored) port bauds."""
    before = {n: snapshot(bench, n) for n in wcbs}
    failure = None
    try:
        yield before
    except Exception as e:  # noqa: BLE001 — re-raised below after the leak check
        failure = e
    leaks = []
    for n in wcbs:
        try:
            after = snapshot(bench, n)
        except AssertionError:
            try:
                after = snapshot(bench, n)
            except AssertionError as e:
                leaks.append(f"W{n}: config could not be re-read to check it ({e})")
                continue
        bench.cache[n] = after
        if after != before[n]:
            missing = [t for t in before[n] if t not in after]
            extra = [t for t in after if t not in before[n]]
            sent, left = [], []
            try:
                sent, left = _auto_restore(bench, n, before[n], after)
                if sent:
                    after = snapshot(bench, n)
                    bench.cache[n] = after
            except AssertionError as e:      # a send or the re-read timed out: report the leak with what was tried
                left.append(f"auto-restore stopped: {e}")
            what = f"W{n}: missing {missing} / extra {extra}"
            if after == before[n]:
                leaks.append(f"{what} — put back by config_guard: {sent}")
            else:
                still = ([t for t in before[n] if t not in after], [t for t in after if t not in before[n]])
                leaks.append(what + (f" — config_guard sent {sent}, board still differs: missing {still[0]} / extra {still[1]}"
                                     if sent else "") + (f"; not put back: {left}" if left else ""))
        bench.links.resync(n)
    if leaks:
        bench.note("CONFIG LEAK " + " | ".join(leaks))
        prefix = f"{failure}\n" if failure else ""
        raise AssertionError(prefix + "CONFIG NOT RESTORED — " + " | ".join(leaks))
    if failure:
        raise failure


# ------------------------------------------------------------------ probe mesh mode
def mesh_params(bench):
    """Mesh settings for probe.mesh_join, read from W1's config chain (WCB.ino:3143-3167, 3244). The password is
    never printed or noted; session logs already carry it and results/ is gitignored."""
    tokens = snapshot(bench, bench.usb_wcb_number())

    def value(prefix):
        t = token(tokens, prefix)
        if not t:
            from hil.runner import Skip
            raise Skip(f"W1 config lacks {prefix}")
        return t[len(prefix):]

    return dict(oct2=int(value("?MAC,2,"), 16), oct3=int(value("?MAC,3,"), 16), password=value("?EPASS,"),
                channel=int(value("?WCBCH,")), quantity=int(value("?WCBQ,")), checksum=value("?ETM,CHKSM,").upper() == "ON")


# The two WCBs, W1's persisted learned peers 6 and 9, the old MgmtRelay (19) and NaviCore (20).
FORBIDDEN_MESH_IDS = (1, 2, 6, 9, 19, 20)


@contextmanager
def probe_in_mesh(bench, probe_name, device_id, forget=True, **overrides):
    """Join <probe_name> to the mesh as a TEMPORARY client (never persisted by any WCB) and leave on exit.

    Leaving reboots the probe, so its wires are released first; they re-bind on next use (a joined probe still
    carries serial channels). forget=True then drops the stale temporary peer on every WCB with ?WDP,FORGET — after
    the leave, since a FORGET before it is undone by the next advert. With forget=False the WCBs keep the peer, and
    expect its broadcast ACKs, until they evict it 50 s later (WCB.ino:508).

    Ids: never a WCB's, NaviCore's, or a persisted learned peer's — a temporary advert from a learned id downgrades it
    and persists the removal (WCB_WDP.cpp:658-669). A WCB clears a sender's duplicate ring only on a boot announce,
    which clients never send (WCB.ino:4219-4228), so a test that sends commands uses an id no other test has used
    since that WCB's last boot."""
    from hil.runner import Skip
    if device_id in FORBIDDEN_MESH_IDS:
        raise AssertionError(f"mesh id {device_id} is reserved on this bench")
    w = WCB(bench.dev("wcb1"))
    row = next((x for x in w.run("?WDP,DUMP", timeout=8) if x.startswith(f"[WDP:N={device_id},")), "")
    if "PEER=2" in row:
        raise Skip(f"mesh id {device_id} is a persisted learned peer on W1")
    params = {**mesh_params(bench), **overrides}
    probe = bench.probe(probe_name)
    for l in bench.links.all():
        if l.probe_name == probe_name:
            bench.links.release(l)
    probe.mesh_join(device_id, params["oct2"], params["oct3"], params["password"], params["quantity"],
                    channel=params["channel"], checksum=params["checksum"], temporary=True)
    try:
        yield probe
    finally:
        try:
            if probe.mesh_id:            # a test that timed its own probe.mesh_leave() has already left
                probe.mesh_leave()
        finally:                         # even when the leave fails, or the next test finds a stale temporary peer
            bench.links.forget_probe(probe_name)
            if forget:
                w.run(f"?WDP,FORGET,{device_id}")
                for n in remote_wcbs(bench):
                    w.send(f";W{n},?WDP,FORGET,{device_id}")
                time.sleep(1.0)
