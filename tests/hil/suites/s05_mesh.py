""";W routing, ETM accounting, MGMT fragments and the remote terminal (W1 -> W2).

The sections after the original tests follow docs/hil_plan/WCB.md: WCB-WP13 (?MGMT,FRAG on the target), WCB-WP31 (the
remote terminal) and WCB-WP54 (;W routing forms). Every multi-chunk FRAG is one unacknowledged ESP-NOW broadcast per
chunk (handleMgmtForward, WCB.ino), so those tests check the outcome and send again under a new session id when a chunk
was lost, instead of failing the firmware for the radio.
"""
import difflib
import re
import secrets
import time
import zlib
from contextlib import contextmanager

from hil.runner import Skip, test
from hil.wcb import WCB, chain_crc
from suites.common import (Console, Watch, config_guard, link, marker, nonce, padded, probe_in_mesh, quiet_lines,
                           require_tokens, snapshot, token, usb_wcb, wire)


@test("mesh.unicast_acked", ";W2 unicast is delivered and ACKed exactly once in W1's ETM stats",
      needs=["wcb1", "probe2"])
def unicast_acked(bench):
    w = usb_wcb(bench)
    probe, ch = wire(bench, 2, "S2")
    before = w.etm_board_stats()[2]
    t = marker()
    m = probe.dev.mark()
    w.send(f";W2,;S2{t}")
    probe.expect_bytes(ch, t.encode() + b"\r", timeout=3, since=m)
    time.sleep(1.0)
    after = w.etm_board_stats()[2]
    bench.note(f"W2 ETM stats before {before} after {after}")
    assert (after["sent"], after["ackd"]) == (before["sent"] + 1, before["ackd"] + 1), f"expected exactly one more unicast, sent and ACKed: {before} -> {after}"
    assert after["failed"] == before["failed"], f"failures rose: {before} -> {after}"


@test("mesh.alias", ";W<alias>,<cmd> routes by the remote board's alias", needs=["wcb1", "probe2"])
def alias(bench):
    alias_tok = token(snapshot(bench, 2), "?ALIAS,")
    if not alias_tok:
        from hil.runner import Skip
        raise Skip("W2 has no alias")
    name = alias_tok.split(",", 1)[1]
    probe, ch = wire(bench, 2, "S2")
    t = marker()
    m = probe.dev.mark()
    usb_wcb(bench).send(f";W{name},;S2{t}")
    probe.expect_bytes(ch, t.encode() + b"\r", timeout=3, since=m)


@test("mesh.unreachable", ";W to a board that is not a peer says so", needs=["wcb1"])
def unreachable(bench):
    lines = usb_wcb(bench).run(";W15,;S1x")
    assert any("WCB 15 is not a reachable target" in t for t in lines), f"got {lines}"


@test("mesh.chain_split", "Characterisation: ;W2,a^b sends only a to W2 and runs b locally",
      needs=["wcb1", "probe1", "probe2"])
def chain_split(bench):
    p2, c2 = wire(bench, 2, "S2")
    p1, c1 = wire(bench, 1, "S1")
    a, b = marker("a"), marker("b")
    m1, m2 = p1.dev.mark(), p2.dev.mark()
    usb_wcb(bench).send(f";W2,;S2{a}^;S1{b}")
    p2.expect_bytes(c2, a.encode() + b"\r", timeout=3, since=m2)
    p1.expect_bytes(c1, b.encode() + b"\r", timeout=3, since=m1)


@test("mesh.frag_chain", "?MGMT,FRAG runs a whole ^ chain on the target board", needs=["wcb1", "probe2"])
def frag_chain(bench):
    probe, ch = wire(bench, 2, "S2")
    a, b = marker("a"), marker("b")
    m = probe.dev.mark()
    usb_wcb(bench).send(f"?MGMT,FRAG,2,A1,0,1,;S2{a}^;S2{b}")
    probe.expect_bytes(ch, f"{a}\r{b}\r".encode(), timeout=4, since=m)


@test("mesh.rterm", "?RTERM mirrors W2's console to W1 as [TERM:2] lines", needs=["wcb1"])
def rterm(bench):
    w = usb_wcb(bench)
    fw = w.version()
    with w.remote_terminal(2, 1):
        lines = w.remote_run(2, "?VERSION", "End of Version")
    assert f"Software Version: {fw}" in lines, f"W2 terminal said {lines}, W1 runs {fw}"


# ============================================================ WCB-WP13: ?MGMT,FRAG on the target
FRAG_CHUNK = 179        # MGMT_PAYLOAD_SIZE 180 less the NUL: the relay refuses a longer chunk (handleMgmtForward, WCB.ino)
FRAG_GAP_S = 0.25       # the Wizard's MGMT_CHUNK_DELAY between the chunks of a push (Wizard/app.js)
FRAG_STALE_S = 2.5      # a new id takes over an unfinished session only after 2 s without a chunk (handleMgmtPacket)
FILL = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 20
HELP_PAGE = "Command Reference"     # the title line of printCommandHelp's top-level page (WCB_Help.cpp)


def _sid():
    """A fresh FRAG session id (4 hex digits). The target drops every chunk of its last 8 completed ids for good
    (mgmtRecentIds, WCB.ino), and never FFFF: that ring starts filled with 0xFF bytes (WCB_Client skips it too)."""
    while True:
        sid = nonce()[:4]
        if sid != "FFFF":
            return sid


def _chunk(w, target, sid, i, total, text):
    w.send(f"?MGMT,FRAG,{target},{sid},{i},{total},{text}")


def _push(w, target, sid, chunks, gap=FRAG_GAP_S):
    """Send `chunks` as one FRAG session through W1, `gap` apart as the Wizard does -> host time of the last send."""
    for i, c in enumerate(chunks):
        if i:
            time.sleep(gap)
        _chunk(w, target, sid, i, len(chunks), c)
    return time.monotonic()


def _split(payload):
    return [payload[i:i + FRAG_CHUNK] for i in range(0, len(payload), FRAG_CHUNK)]


def _arrived(watch, l, data, timeout):
    """True once `data` is on wire `l` since the watch's mark; False after `timeout`."""
    try:
        watch.expect(l, data, timeout=timeout)
        return True
    except AssertionError:
        return False


def _has_line(lines, pattern):
    rx = re.compile(pattern)
    return any(rx.search(x) for x in lines)


def _at(dev, pattern, since):
    """Host arrival time of the first console line at or after `since` that matches `pattern`, or None."""
    rx = re.compile(pattern)
    return next((ts for ts, x in list(dev.lines[since:]) if rx.search(x)), None)


def _await_line(dev, pattern, since, timeout, what):
    """Wait for a console line matching `pattern` at or after `since` -> its index. The failure names `what` and a line
    count and quotes no line: around a ?backup the console carries ?EPASS and ?WIFI (SerialDevice.expect quotes the
    last 15 lines)."""
    rx = re.compile(pattern)
    deadline = time.monotonic() + timeout
    while True:
        for i, x in enumerate(dev.since(since)):
            if rx.search(x):
                return since + i
        if time.monotonic() >= deadline:
            raise AssertionError(f"{dev.name}: no {what} within {timeout:g} s "
                                 f"({dev.mark() - since} lines since, not quoted)")
        time.sleep(0.05)


def _on_w2(bench, cmd):
    """One command on W2: on its own USB when it has one (waited for), otherwise as ;W2,<cmd> (no ^ in `cmd`)."""
    own = bench.usb_wcbs().get(2)
    if own:
        return WCB(bench.dev(own)).run(cmd, timeout=8)
    usb_wcb(bench).send(f";W2,{cmd}")
    time.sleep(0.5)
    return []


def _put_back_w2(bench, before, ports, keys=()):
    """Undo a push on W2: each label in `ports` back to its value in the snapshot `before` (or cleared), each
    sequence key cleared."""
    for p in ports:
        orig = token(before, f"?LABEL,{p},")
        _on_w2(bench, orig if orig else f"?LABEL,CLEAR,{p}")
    for k in keys:
        _on_w2(bench, f"?SEQ,CLEAR,{k}")


def _peers_back(bench, w, wait=25.0):
    """After W2 restarted: ?WDP,POLL on W1 (every peer advertises, and any packet marks its sender online, so W2 sees
    W1 again at once instead of at W1's next heartbeat), then wait until W1's ETM table shows W2 online, as s99's
    _peers_online does. Notes a failure and never raises: it runs where the test's own result must stand."""
    try:
        w.run("?WDP,POLL")
        time.sleep(1.0)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if w.etm_board_stats().get(2, {}).get("online"):
                return
            time.sleep(1.0)
        bench.note(f"W1 still saw WCB2 offline {wait:.0f} s after its restart")
    except AssertionError as e:
        bench.note(f"after W2's restart the peer check failed: {(str(e).splitlines() or [repr(e)])[0]}")


def _push_payload(key, size=450):
    """The remote push of WCB-WP13 row 1, as one ^ chain: S3's label set first and last (so the outcome shows the chain
    ran in order), 30-character labels at most (the ?LABEL handler refuses more), and a sequence whose value holds a ^
    (a ?SEQ,SAVE value runs to the next ^? - parseCommandsNoChecksum) padded so the chain needs three chunks.
    -> (payload, the tokens W2's ?backup must then hold, the first S3 label that must not survive, the value's
    markers)."""
    la, lb, first, last = padded("a", 30), marker("b"), marker("c"), marker("c")
    mx, my = marker("x"), marker("y")

    def build(pad):
        value = f";S2{mx}^;S2{my}{pad}"
        return value, "^".join((f"?LABEL,S3,{first}", f"?LABEL,S5,{la}", f"?LABEL,S4,{lb}",
                                f"?SEQ,SAVE,{key},{value}", f"?LABEL,S3,{last}"))

    _, base = build("")
    value, payload = build(FILL[:max(0, size - len(base))])
    want = [f"?LABEL,S3,{last}", f"?LABEL,S4,{lb}", f"?LABEL,S5,{la}", f"?SEQ,SAVE,{key},{value}"]
    return payload, want, f"?LABEL,S3,{first}", (mx, my)


def _push_problems(tokens, want, stale):
    missing = [t[:40] for t in want if t not in tokens]
    out = []
    if missing:
        out.append(f"W2's config lacks {missing}" + ("" if len(missing) == len(want)
                                                     else " - the chain was applied only in part"))
    if stale in tokens:
        out.append("S3 kept the label the chain set FIRST: its commands did not run in order")
    return out


@test("mesh.frag_push_chain", "A three-chunk ?MGMT,FRAG push (the Wizard's remote push) applies its whole ^ chain on W2 in order: three labels, S3's set twice, and a sequence whose value holds a ^, stored whole and not run", needs=["wcb1"], links=[])
def frag_push_chain(bench):
    """WCB-WP13 row 1 (wcb.mgmt.relay_push_multichunk_chain). The relay broadcasts each chunk once, unacknowledged
    (handleMgmtForward), and the target runs the chain only when every chunk is in (handleMgmtPacket), through
    parseCommandsAndEnqueue, so a push is applied whole or not at all: after a lost chunk nothing changed, and the push
    goes again under a new id, which takes over once the unfinished session is 2 s stale (the snapshot outlasts that).
    W2 S2 is watched, when wired, for the value's two ;S2 markers: stored, never run."""
    w = usb_wcb(bench)
    s2 = bench.links.get(2, "S2")
    key = f"HILPU{nonce()[:4]}"
    payload, want, stale, markers = _push_payload(key)
    chunks = _split(payload)
    problems = []
    with config_guard(bench, 2) as before:
        try:
            watch = Watch(s2)
            tokens, pushes = [], 0
            for pushes in range(1, 4):
                _push(w, 2, _sid(), chunks)
                time.sleep(1.0)
                tokens = snapshot(bench, 2)
                if any(t in tokens for t in want):
                    break
                bench.note(f"push {pushes}/3 changed nothing on W2 (a broadcast chunk lost?); pushing again")
            bench.note(f"{len(payload)} characters in {len(chunks)} chunks; W2 changed after push {pushes}")
            problems += _push_problems(tokens, want, stale)
            if s2 is not None:
                ran = [x for x in markers if x.encode() in watch.got(s2)]
                if ran:
                    problems.append(f"the stored value ran: {ran} reached W2 S2")
        finally:
            _put_back_w2(bench, before[2], ("S3", "S4", "S5"), (key,))
    assert not problems, "; ".join(problems)


@test("mesh.frag_push_reboot", "A pushed chain ending in ?reboot restarts W2 exactly once, at least 4 s after the last chunk, and every setting in the chain is there after the boot (W2 reboot)", needs=["wcb1", "wcb2"], links=[])
def frag_push_reboot(bench):
    """WCB-WP13 row 1, the reboot variant: the Wizard appends ?reboot to a push that needs one (Wizard/app.js). CLAUDE.md
    rule 11: reboot() only sets rebootPending, and loop() restarts once the command queue has been quiet for
    PWM_REBOOT_QUIET_MS (4 s), printing 'Rebooting now...' (WCB.ino), so none of the chain's commands is lost to the
    restart. The restart is timed on W2's own USB from the host time of the last chunk, which can only make it look
    later. Restores W2 as found: W1 sees it online again (?WDP,POLL), and the labels and the sequence are undone."""
    own = bench.usb_wcbs().get(2)
    if not own:
        raise Skip("W2 has no USB console here: its restart cannot be seen")
    d2 = bench.dev(own)
    w = usb_wcb(bench)
    key = f"HILPR{nonce()[:4]}"
    payload, want, stale, _ = _push_payload(key)
    chunks = _split(payload + "^?reboot")
    problems = []
    with config_guard(bench, 2) as before:
        try:
            m2 = d2.mark()
            for pushes in range(1, 4):
                sent = _push(w, 2, _sid(), chunks)
                try:
                    _await_line(d2, r"^Reboot queued", m2, 6, "'Reboot queued' on W2")
                    break
                except AssertionError:
                    bench.note(f"push {pushes}/3 did not reach W2 (no 'Reboot queued'; a broadcast chunk lost?); "
                               "pushing again")
            else:
                raise AssertionError("W2 never ran the pushed chain in 3 pushes: no 'Reboot queued' on its USB")
            _await_line(d2, r"^Rebooting now", m2, WCB.REBOOT_DEFER_S, "'Rebooting now' on W2")
            took = _at(d2, r"^Rebooting now", m2) - sent
            WCB(d2).wait_boot(m2, timeout=30)
            _peers_back(bench, w)
            tokens = snapshot(bench, 2)
            lines = d2.since(m2)
            bench.note(f"W2 restarted {took:.1f} s after the last of {len(chunks)} chunks (push {pushes})")
            if took < 4.0:
                problems.append(f"W2 restarted {took:.1f} s after the last chunk: under the 4 s quiet window")
            for what in ("Reboot queued", "Rebooting now"):
                n = sum(1 for x in lines if x.startswith(what))
                if n != 1:
                    problems.append(f"{n} '{what}' lines on W2, expected exactly one")
            problems += _push_problems(tokens, want, stale)
        finally:
            _put_back_w2(bench, before[2], ("S3", "S4", "S5"), (key,))
    assert not problems, "; ".join(problems)


@test("mesh.frag_origin_rebroadcast", "Plain text that reaches W2 through ?MGMT,FRAG is re-broadcast by W2 - once on W2 S3 and, over the mesh, once on W1 S2 - in one chunk and in two; the same text sent board-to-board with ;W2 stays on W2", needs=["wcb1"])
def frag_origin_rebroadcast(bench):
    """WCB-WP13 row 2 (wcb.rx.wizard_origin_rebroadcast, wcb.loopprev.wizard_origin_broadcasts,
    wcb.mgmt.frag_origin_rebroadcast, wcb.cmd.frag_wizard_origin_rebroadcast). A one-chunk FRAG rides an ETM unicast
    whose payload starts with SOH (handleMgmtForward); the target strips it and enqueues with origin 0
    (espNowReceiveCallback), so processBroadcastCommand sends the text on to the mesh. A multi-chunk FRAG is type-3
    packets, which handleMgmtPacket enqueues with origin 0 as well. ;W2,<text> is board-to-board ETM with no marker:
    origin 1, W2's own ports only (CLAUDE.md rule 3). The two-chunk text is 184 characters: over one chunk, and within
    the 187 a mesh command may carry under ?ETM,CHKSM (ETM_MAX_CMD_WITH_CRC), so W2 can re-broadcast it whole. Arm (d)
    of the row, a client's fragmented unicast (type 5) staying local, belongs to s19 and is not here."""
    far, near = link(bench, 2, "S3"), link(bench, 1, "S2")
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON")
    w = usb_wcb(bench)
    problems = []

    def arm(name, t, send, rebroadcast):
        """False when the text never reached W2 S3; otherwise counts it on both wires after a settle."""
        data = t.encode() + b"\r"
        watch = Watch(far, near)
        send()
        if not _arrived(watch, far, data, 4):
            return False
        if rebroadcast:
            _arrived(watch, near, data, 4)
        time.sleep(1.5)
        n_far, n_near = watch.got(far).count(data), watch.got(near).count(data)
        if n_far != 1:
            problems.append(f"{name}: the text arrived {n_far} times on W2 S3, expected once")
        if n_near != (1 if rebroadcast else 0):
            problems.append(f"{name}: the text arrived {n_near} times on W1 S2, expected "
                            + ("once (W2's re-broadcast)" if rebroadcast else "never (board-to-board text stays on W2)"))
        return True

    # (a) one chunk: the SOH-marked ETM unicast
    t = marker("bc")
    if not arm("one chunk", t, lambda: w.send(f"?MGMT,FRAG,2,{_sid()},0,1,{t}"), True):
        problems.append("one chunk: the text never reached W2 S3")

    # (b) two chunks: a type-3 session, sent again under a new id when a chunk was lost
    for attempt in range(1, 4):
        t = padded("bc", 184)
        if arm("two chunks", t, lambda: _push(w, 2, _sid(), _split(t), gap=0.2), True):
            break
        bench.note(f"two-chunk FRAG {attempt}/3 never reached W2 S3 (a broadcast chunk lost?); sending again")
    else:
        problems.append("two chunks: the text never reached W2 S3 in 3 tries")

    # (c) control: board-to-board text is mesh-originated on W2 and goes no further
    t = marker("bc")
    if not arm(";W2", t, lambda: w.send(f";W2,{t}"), False):
        problems.append(";W2: the text never reached W2 S3")
    assert not problems, "; ".join(problems)


@test("mesh.frag_trailing_q", "A ?MGMT,FRAG line ending in '?' is forwarded, not eaten as a help request: a chain split right after a '?', a sequence value split at its '?' (read back byte for byte), and a one-chunk ;S2<text>?", needs=["wcb1"])
def frag_trailing_q(bench):
    """WCB-WP13 row 3 (wcb.cmd.mgmt_frag_trailing_q_exempt, wcb.cmd.mgmt_frag_trailing_q). processLocalCommand (WCB.ino)
    takes any ?-command ending in '?' for a help request, except the data-bearing verbs (MGMT, SEQ, FUNCCHAR, CMDCHAR,
    DELIM, WIFI): a push chunk boundary can land just after a '?', and such a chunk used to print as help and the push
    died at the 15 s session timeout. W1 is the relay, so an eaten chunk would print the help page on W1 and never
    reach W2. The sequence value is read back with ?MGMT,SEQGET, whose reply frags are unacknowledged broadcasts too
    (retried 2 s apart: W2 ignores a repeat for 1.5 s after answering, handleMgmtSeqValRequest)."""
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []

    def help_on_w1(m):
        """What W1 printed since `m` that says it ate the chunk: its help page, or the oversize refusal."""
        return [x for x in quiet_lines(w.dev, m) if HELP_PAGE in x or "REJECTED" in x][:2]

    # (1) a chain split right after '?': ";S2<a>^?" + "VERSION^;S2<b>"
    for attempt in range(1, 4):
        a, b = marker("a"), marker("b")
        watch = Watch(s2)
        m = w.dev.mark()
        _push(w, 2, _sid(), [f";S2{a}^?", f"VERSION^;S2{b}"], gap=0.2)
        got_a = _arrived(watch, s2, f"{a}\r".encode(), 4)
        bad = help_on_w1(m)
        if bad:
            problems.append(f"split after '?': W1 printed {bad} instead of forwarding the chunk")
            break
        if got_a:
            if not _arrived(watch, s2, f"{b}\r".encode(), 2):
                problems.append("split after '?': only the command before the split ran")
            break
        bench.note(f"split-after-'?' push {attempt}/3 never reached W2 S2 (a broadcast chunk lost?); again")
        time.sleep(1.0)
    else:
        problems.append("split after '?': nothing reached W2 S2 in 3 tries")

    # (2) a sequence value with its '?' as the 179th character, so the first chunk ends in it
    key = f"HILQF{nonce()[:4]}"
    head = f"?SEQ,SAVE,{key},;S2{marker('q')}"
    payload = head + FILL[:FRAG_CHUNK - 1 - len(head)] + "?" + marker("r")
    value = payload[len(f"?SEQ,SAVE,{key},"):]
    assert payload[FRAG_CHUNK - 1] == "?" and _split(payload)[0].endswith("?")
    with config_guard(bench, 2):
        try:
            line, bad = None, []
            for attempt in range(1, 4):
                m = w.dev.mark()
                _push(w, 2, _sid(), _split(payload), gap=0.2)
                time.sleep(2.0)
                bad = help_on_w1(m)
                if bad:
                    problems.append(f"sequence split at its '?': W1 printed {bad} instead of forwarding the chunk")
                    break
                line = _seqget(w, key)
                if line is not None and ",NOTFOUND," not in line:
                    break
                bench.note(f"sequence push {attempt}/3: W2 answered {'nothing' if line is None else 'NOTFOUND'}; again")
            if not bad and line != f"[MGMT:SEQVAL,2]{key},OK,{value}":
                problems.append(f"sequence split at its '?': ?MGMT,SEQGET gave {len(line or '')} characters "
                                f"({(line or '')[:60]!r}...), expected {key},OK,<the {len(value)}-character value>")
        finally:
            _on_w2(bench, f"?SEQ,CLEAR,{key}")

    # (3) one chunk ending in '?': the relay's ETM path, and ;S2 writes the '?' too
    c = marker("c")
    watch = Watch(s2)
    m = w.dev.mark()
    w.send(f"?MGMT,FRAG,2,{_sid()},0,1,;S2{c}?")
    if not _arrived(watch, s2, f"{c}?\r".encode(), 4):
        problems.append(f"one chunk ending in '?': W2 S2 got {watch.got(s2)!r}, expected <c>?CR")
    bad = help_on_w1(m)
    if bad:
        problems.append(f"one chunk ending in '?': W1 printed {bad}")
    assert not problems, "; ".join(problems)


def _seqget(w, key, tries=3):
    """?MGMT,SEQGET,2,<key> on W1 -> the [MGMT:SEQVAL,2]<key>,... line, or None when no answer came in `tries` asks 2 s
    apart (the reply frags are broadcast once each, unacknowledged)."""
    for i in range(tries):
        if i:
            time.sleep(2.0)
        m = w.dev.mark()
        w.send(f"?MGMT,SEQGET,2,{key}")
        try:
            return w.dev.expect(rf"^\[MGMT:SEQVAL,2\]{re.escape(key)},", timeout=4, since=m).string.rstrip()
        except AssertionError:
            pass
    return None


@test("mesh.frag_trailing_q_mixed_case", "(should) A ?Mgmt,FRAG line ending in '?' is forwarded like ?MGMT,FRAG: the trailing-'?' exemption matches the verb in any case, as the dispatcher does", needs=["wcb1"])
def frag_trailing_q_mixed_case(bench):
    """processLocalCommand (WCB.ino) dispatches every verb case-insensitively (rootUpper == "MGMT"), and the chain
    splitter matches ?MGMT in any case (tokenHasVerb), but the trailing-'?' exemption tests only the spellings 'MGMT,'
    and 'mgmt,' (message.startsWith). So ?Mgmt,FRAG,...,;S2<c>? is taken for a help request: W1 prints the help page
    and nothing is forwarded. ?Seq,Save,<key>,<value ending in ?> and the other exempt verbs (FUNCCHAR, CMDCHAR, DELIM,
    WIFI) share the gap. Expected: <c>? on W2 S2 and no help page on W1."""
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    c = marker("c")
    watch = Watch(s2)
    m = w.dev.mark()
    w.send(f"?Mgmt,FRAG,2,{_sid()},0,1,;S2{c}?")
    ok = _arrived(watch, s2, f"{c}?\r".encode(), 4)
    helped = any(HELP_PAGE in x for x in w.dev.since(m))
    assert ok and not helped, (f"?Mgmt,FRAG ending in '?': W2 S2 got {watch.got(s2)!r}"
                               f"{', and W1 printed its help page' if helped else ''}")


@test("mesh.frag_sessions", "?MGMT,FRAG sessions on W2: another id is refused while the session is under 2 s old, takes over once it is stale, an unfinished session is dropped at 15 s, and a session for another board is ignored", needs=["wcb1"])
def frag_sessions(bench):
    """WCB-WP13 row 4 (wcb.mgmt.frag_session_busy_timeout). handleMgmtPacket (WCB.ino) keeps one session: a chunk with
    another id is refused while the session had a chunk under 2 s ago ('[MGMT] Session <A> busy - rejecting chunk
    from session <B>') and starts a new session after that; checkMgmtTimeout, from loop(), drops a session 15 s
    (MGMT_SESSION_TIMEOUT_MS) after its last chunk ('... timed out - discarding'); a packet for another board is
    dropped before any of that, unlogged. The lines print only under ?DEBUG,MGMT (RAM only), on W2's console. Every
    chunk is one unacknowledged broadcast, so each arm first reads from W2's own log that the chunks it depends on
    arrived, and runs again under new ids when one was lost. B's lone second chunk opens a session of its own that
    holds the next id off for 2 s, which the busy arm waits out."""
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with Console(bench, 2) as c2:
        m = c2.send("?DEBUG,MGMT,ON")
        c2.expect(r"^MGMT debugging enabled", timeout=4, since=m)
        try:
            # 1. busy: A0, B0, A1, B1 inside half a second
            ok = False
            for attempt in range(1, 4):
                A, B = _sid(), _sid()
                a1, a2, b1, b2 = marker("a"), marker("a"), marker("b"), marker("b")
                watch = Watch(s2)
                cm = c2.mark()
                for sid, i, text in ((A, 0, f";S2{a1}^"), (B, 0, f";S2{b1}^"), (A, 1, f";S2{a2}"), (B, 1, f";S2{b2}")):
                    _chunk(w, 2, sid, i, 2, text)
                    time.sleep(0.15)
                time.sleep(FRAG_STALE_S)            # the chain runs, and B's lone second chunk goes stale
                log = c2.lines(cm)
                ok = (_has_line(log, rf"Session {A} busy\b.*\bsession {B}\b")
                      and _has_line(log, rf"Session {A} complete"))
                if ok:
                    got = watch.got(s2)
                    if not (a1.encode() in got and a2.encode() in got):
                        problems.append(f"busy: session A completed but its chain did not reach W2 S2: {got!r}")
                    if b1.encode() in got or b2.encode() in got:
                        problems.append("busy: session B ran although W2 refused its first chunk")
                    break
                bench.note(f"busy arm {attempt}/3 inconclusive: W2 did not log both 'A busy for B' and 'A complete' "
                           "(a chunk lost?); again")
            if not ok:
                problems.append("busy: no try in 3 had A's two chunks and B's first chunk all reach W2")

            # 2. stale takeover: C0 only, 3 s, then a whole D
            for attempt in range(1, 4):
                C, D = _sid(), _sid()
                c1, d1, d2 = marker("c"), marker("d"), marker("d")
                watch = Watch(s2)
                cm = c2.mark()
                _chunk(w, 2, C, 0, 2, f";S2{c1}^")
                time.sleep(3.0)
                _chunk(w, 2, D, 0, 2, f";S2{d1}^")
                time.sleep(0.15)
                _chunk(w, 2, D, 1, 2, f";S2{d2}")
                time.sleep(1.5)
                log = c2.lines(cm)
                if _has_line(log, rf"Session {C} busy"):
                    problems.append("stale: W2 refused D for session C, 3 s after C's only chunk")
                    break
                if _has_line(log, rf"New session {C}\b") and _has_line(log, rf"Session {D} complete"):
                    got = watch.got(s2)
                    if not (d1.encode() in got and d2.encode() in got):
                        problems.append(f"stale: session D completed but its chain did not reach W2 S2: {got!r}")
                    if c1.encode() in got:
                        problems.append("stale: the unfinished session C ran")
                    break
                bench.note(f"stale arm {attempt}/3 inconclusive: W2 did not log C's start and D's completion; again")
                time.sleep(FRAG_STALE_S)
            else:
                problems.append("stale: no try in 3 had C's chunk and both of D's reach W2")

            # 3. a session for WCB3 (not on the bench) is none of W2's business
            F = _sid()
            watch = Watch(s2)
            cm = c2.mark()
            _chunk(w, 3, F, 0, 2, f";S2{marker('f')}^")
            time.sleep(0.15)
            _chunk(w, 3, F, 1, 2, f";S2{marker('f')}")
            time.sleep(1.5)
            if watch.got(s2):
                problems.append(f"a session for WCB3 put {watch.got(s2)!r} on W2 S2")
            if _has_line(c2.lines(cm), rf"[Ss]ession {F}\b"):
                problems.append("W2 logged a session addressed to WCB3")

            # 4. timeout: E0 only; W2 drops the session 15 s after it
            for attempt in range(1, 4):
                E = _sid()
                watch = Watch(s2)
                cm = c2.mark()
                _chunk(w, 2, E, 0, 2, f";S2{marker('e')}^")
                try:
                    c2.expect(rf"^\[MGMT\] New session {E}\b", timeout=2, since=cm)
                except AssertionError:
                    bench.note(f"timeout arm {attempt}/3: E's chunk never reached W2; again")
                    continue
                try:
                    c2.expect(rf"^\[MGMT\] Session {E} timed out", timeout=19, since=cm)
                except AssertionError:
                    problems.append("timeout: W2 never dropped the unfinished session (no 'timed out' within 19 s)")
                    break
                pre = re.escape(c2.prefix)
                age = (_at(c2.dev, rf"^{pre}\[MGMT\] Session {E} timed out", cm)
                       - _at(c2.dev, rf"^{pre}\[MGMT\] New session {E}\b", cm))
                bench.note(f"W2 dropped the unfinished session {age:.1f} s after its only chunk")
                if not 14.5 <= age <= 16.5:
                    problems.append(f"timeout: dropped {age:.1f} s after its chunk, expected about 15 s")
                if watch.got(s2):
                    problems.append(f"timeout: the unfinished session put {watch.got(s2)!r} on W2 S2")
                break
            else:
                problems.append("timeout: E's chunk never reached W2 in 3 tries")
        finally:
            c2.send("?DEBUG,MGMT,OFF")
            time.sleep(0.5)
    assert not problems, "; ".join(problems)


# ============================================================ WCB-WP31: the remote terminal (RTERM)
TERM_PIECE = 160        # RTERM_TEXT_SIZE (WCB_RemoteTerm.h): the mirror sends a console line in pieces of 160 bytes
IDF_LOG = re.compile(r"^[EWID] \(\d+\) \S+:")     # ESP-IDF log lines reach UART0 without passing through WCBSerial
CHAIN_TAIL = re.compile(r"^(.*)\^.CHK([0-9A-Fa-f]{8})$", re.S)
CONFIGISH = re.compile(r"\^|EPASS|WIFI|PASS", re.I)


def _pieces(line):
    """The [TERM:n] lines a relay prints for one console line. WCBSerial::_bufChar (WCB_RemoteTerm.cpp) drops CR and
    sends its buffer at LF or once it holds 160 bytes; rtermRelayDrain strips the LF and prints nothing for an empty
    piece. So an empty line vanishes, a longer one arrives in 160-byte pieces, and one of exactly 160*k bytes ends
    without an empty piece. A piece is decoded on its own, as the relay's copy is: a UTF-8 character cut at a piece
    boundary reads as U+FFFD there."""
    raw = line.encode("utf-8")
    return [raw[i:i + TERM_PIECE].decode("utf-8", errors="replace") for i in range(0, len(raw), TERM_PIECE)]


def _safe(line):
    """A console line fit for a message: anything that may be config text (it carries ?EPASS and ?WIFI) only as its
    length and CRC."""
    if CONFIGISH.search(line):
        return f"<{len(line)} chars, CRC {zlib.crc32(line.encode()) & 0xFFFFFFFF:08X}>"
    return repr(line[:70])


def _settle(dev, since, idle=1.0, timeout=15.0):
    """Wait until `dev` has printed a line since `since` and then nothing for `idle` seconds."""
    deadline = time.monotonic() + timeout
    seen, changed = dev.mark(), time.monotonic()
    while True:
        now, n = time.monotonic(), dev.mark()
        if n != seen:
            seen, changed = n, now
        if n > since and now - changed >= idle:
            return
        if now >= deadline:
            raise AssertionError(f"{dev.name}: output did not settle within {timeout:g} s ({n - since} lines since)")
        time.sleep(0.05)


def _mirror(w, d2, cmd):
    """Run <cmd> on W2 (as ;W2,<cmd>) inside a W2 -> W1 ?RTERM session -> (the lines W2 printed on its own USB, the
    [TERM:2] lines W1 printed, prefix dropped). Both end at a unique ;S0 echo, sent once W2's USB has gone quiet."""
    m1, m2 = w.dev.mark(), d2.mark()
    w.send(f";W2,{cmd}")
    _settle(d2, m2)
    end = "HILEND" + nonce()
    w.send(f";W2,;S0,{end}")
    i2 = _await_line(d2, rf"^{end}(?![0-9A-F])", m2, 5, f"the {end} echo on W2's USB")
    i1 = _await_line(w.dev, rf"^\[TERM:2\]{end}(?![0-9A-F])", m1, 5, f"the mirrored {end} echo on W1")
    usb = d2.since(m2)[:i2 - m2]
    term = [x[len("[TERM:2]"):] for x in w.dev.since(m1)[:i1 - m1] if x.startswith("[TERM:2]")]
    return usb, term


def _mirror_problems(bench, cmd, usb, term):
    """Compare W1's [TERM:2] lines with W2's USB lines cut the relay's way -> (problems, pieces expected). Every missing
    or unexpected piece goes into the report note; the message carries the first dozen."""
    want = [p for x in usb if x and not IDF_LOG.match(x) for p in _pieces(x)]
    if want == term:
        return [], len(want)
    missing, extra = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=want, b=term, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            missing += [f"#{i} {_safe(want[i])}" for i in range(i1, i2)]
        if op in ("insert", "replace"):
            extra += [f"#{j} {_safe(term[j])}" for j in range(j1, j2)]
    bench.note(f"{cmd} mirror: missing {missing}; unexpected {extra}")
    msg = f"{cmd}: {len(missing)} of {len(want)} mirrored lines missing"
    if missing:
        msg += " (" + "; ".join(missing[:12]) + (" ..." if len(missing) > 12 else "") + ")"
    if extra:
        msg += f", {len(extra)} unexpected (" + "; ".join(extra[:6]) + ")"
    return [msg], len(want)


def _live_chain(lines, pieces):
    """The 'For Configured Boards' chain of a ?backup (printBackupConfig: header line, then the chain on one line) ->
    its text or None. pieces=True re-joins it from [TERM:n] pieces: each full 160-byte piece continues the line."""
    for i, x in enumerate(lines):
        if "=== For Configured Boards" in x:
            if not pieces:
                return lines[i + 1] if i + 1 < len(lines) else None
            text = ""
            for p in lines[i + 1:]:
                text += p
                if CHAIN_TAIL.match(text) or len(p.encode("utf-8")) != TERM_PIECE:
                    break
            return text or None
    return None


def _chain_ok(text):
    m = CHAIN_TAIL.match(text or "")
    return bool(m) and chain_crc(m.group(1)) == m.group(2).upper()


def _nc_wait(nc, pattern, since, timeout=6.0):
    """A NaviCore console line matching `pattern` after `since`, poking the harmless #L12 every second: NaviCore can
    hold a finished line unsent until more output follows it (hil/navicore.py, docs/HIL_TESTING.md §5)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return nc.expect(pattern, timeout=1.0, since=since)
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            nc.send("#L12")


@test("mesh.rterm_long_output", "A W2 -> W1 ?RTERM mirror carries ?HELP and ?backup whole: every line W2 prints on its own USB arrives as [TERM:2] lines, those over 160 bytes in 160-byte pieces, and the mirrored ?backup chain passes its CRC", needs=["wcb1", "wcb2"], links=[])
def rterm_long_output(bench):
    """WCB-WP31 row 1 (rterm.long_output_and_long_lines). While a session is armed, WCBSerial::write copies every byte
    W2 prints into a line buffer, and each piece goes out as one ESP-NOW unicast whose esp_now_send result nobody
    checks; the relay queues each packet 16 deep (RTERM_QUEUE_DEPTH), dropping when full, and prints it from loop()
    (WCB_RemoteTerm.cpp). So W2's own USB, cut the relay's way (_pieces), is exactly what W1 should print. ?HELP is a
    burst of short lines; ?backup has chains of 1-3 KB on one line, and the mirrored 'For Configured Boards' chain,
    re-joined from its pieces, must pass its CRC. A loss is reported line by line (config text by length and CRC only):
    loss under bursts would need a decision (docs/hil_plan/WCB.md WCB-WP31). A ?backup whose chain already fails its
    CRC on W2's own USB (another task's line inside it, docs/HIL_TESTING.md §6) is read once more."""
    own = bench.usb_wcbs().get(2)
    if not own:
        raise Skip("W2 has no USB console here: there is nothing to compare the mirror with")
    d2 = bench.dev(own)
    w = usb_wcb(bench)
    problems, notes = [], []
    with w.remote_terminal(2, bench.usb_wcb_number()):
        time.sleep(0.5)
        for cmd in ("?HELP", "?backup"):
            for attempt in (1, 2):
                usb, term = _mirror(w, d2, cmd)
                if cmd != "?backup" or _chain_ok(_live_chain(usb, False)) or attempt == 2:
                    break
                bench.note("W2's own ?backup chain failed its CRC on its USB (another task's line inside it?); "
                           "running it again")
            found, n = _mirror_problems(bench, cmd, usb, term)
            problems += found
            notes.append(f"{cmd}: {len(usb)} USB lines, {n} pieces expected, {len(term)} mirrored")
            if cmd == "?backup":
                chain = _live_chain(term, True)
                if chain is None:
                    problems.append("?backup: the mirror has no 'For Configured Boards' section")
                elif not _chain_ok(chain):
                    problems.append(f"?backup: the mirrored chain ({len(chain)} characters, re-joined from "
                                    f"{TERM_PIECE}-byte pieces) fails its CRC"
                                    + ("" if _chain_ok(_live_chain(usb, False)) else ", as W2's own USB copy does"))
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("mesh.rterm_relay_navicore", "?RTERM,START,20 on W2 mirrors its console to NaviCore (the Intellex path): NaviCore's USB prints the [TERM:2] session start and a ;W2,?VERSION, W1 gets none of it, and ?RTERM,STOP ends it", needs=["wcb1", "navicore"], links=[])
def rterm_relay_navicore(bench):
    """WCB-WP31 row 2 (rterm.relay_outside_peer_table). WCBSerial::_sendPacket (WCB_RemoteTerm.cpp) registers the relay
    as an ESP-NOW peer on demand before a send, because a relay above the sender's WCBQ is not in its peer table.
    NaviCore relays with the WCB_Client library's processRemoteTerm (WCBClient WCB_Mgmt.h), which prints
    [TERM:<src>]<line> for a packet addressed to it. WCB20 is normally W2's controller (?CONTROLLER,ON,20 registers it
    live), and then the on-demand branch has nothing to do; the note records which case this run was (the firmware
    prints nothing either way)."""
    w = usb_wcb(bench)
    nc = bench.dev("navicore")
    ctrl = token(snapshot(bench, 2), "?CONTROLLER,ON,")
    if ctrl and ctrl.upper() == "?CONTROLLER,ON,20":
        bench.note("WCB20 is W2's controller (?CONTROLLER,ON,20), already an ESP-NOW peer: _sendPacket's on-demand "
                   "registration had nothing to do")
    else:
        bench.note(f"W2's controller setting is {ctrl or 'off'}: WCB20 may be outside W2's peer table, so _sendPacket "
                   "registers it on demand")
    mn = nc.mark()
    w.send(";W2,?RTERM,START,20")
    try:
        _nc_wait(nc, r"^\[TERM:2\]\[RTERM\] Session started", mn)
        m1, mn = w.dev.mark(), nc.mark()         # from here on W2 mirrors to WCB20 only
        w.send(";W2,?VERSION")
        _nc_wait(nc, r"^\[TERM:2\]Software Version: \S", mn)
        _nc_wait(nc, r"^\[TERM:2\]End of Version", mn)
        time.sleep(0.5)
        stray = [x for x in w.dev.since(m1) if x.startswith("[TERM:2]")]
        assert not stray, f"W1 also printed W2's console, mirrored to WCB20: {stray[:3]}"
    finally:
        w.send(";W2,?RTERM,STOP")
        time.sleep(0.5)


@test("mesh.rterm_stop", "?RTERM,STOP ends W2's mirror at once: a ?VERSION after it runs on W2 but no [TERM:2] line reaches W1, and the stop line itself stays on W2's USB", needs=["wcb1"], links=[])
def rterm_stop(bench):
    """WCB-WP31 row 3, the STOP half (rterm.stop_flush_and_password). WCBSerial::stopSession (WCB_RemoteTerm.cpp) flushes
    a pending partial line, clears the relay and then prints '[RTERM] Session stopped' - after the relay is cleared,
    so that line stays on W2's USB. The flush is not reachable from outside: every line a command prints ends in a
    newline, so nothing is pending when a STOP runs. The password half is WCB-WP43. Without a W2 USB console, 1.5 s
    stands in for seeing the STOP run, and the ?VERSION is not seen at all."""
    w = usb_wcb(bench)
    own = bench.usb_wcbs().get(2)
    d2 = bench.dev(own) if own else None
    m = w.send(f";W2,?RTERM,START,{bench.usb_wcb_number()}")
    w.dev.expect(r"^\[TERM:2\]\[RTERM\] Session started", timeout=5, since=m)
    stopped = False
    try:
        w.remote_run(2, "?VERSION", "End of Version")          # control: the session carries W2's console
        m2 = d2.mark() if d2 else None
        w.send(";W2,?RTERM,STOP")
        stopped = True
        if d2:
            d2.expect(r"^\[RTERM\] Session stopped", timeout=4, since=m2)
        else:
            time.sleep(1.5)
        time.sleep(0.3)                     # what W2 mirrored before the STOP ran (an ?DEBUG,ETM receive line) lands first
        m1 = w.dev.mark()
        w.send(";W2,?VERSION")
        if d2:
            d2.expect(r"^End of Version", timeout=4, since=m2)
        time.sleep(3.0)
        term = [x for x in w.dev.since(m1) if x.startswith("[TERM:2]")]
        assert not term, f"W1 still printed W2's console after ?RTERM,STOP: {term[:4]}"
    finally:
        if not stopped:
            w.send(";W2,?RTERM,STOP")
            time.sleep(0.5)


# ============================================================ WCB-WP54: ;W routing forms
AMBIGUOUS = r'^\[;w\] Alias "{}" is ambiguous \(multiple boards\)'     # processWCBMessage (WCB.ino)
CLIENT_IDS = (14, 13, 12, 11, 10, 8, 7, 5, 4, 3)    # for a probe client that sends no commands (mesh.alias_ambiguous_client)


def _alias_of(tokens):
    t = token(tokens, "?ALIAS,")
    return t.split(",", 1)[1] if t else None


def _dump_alias(lines, n):
    """ALIAS of the [WDP:N=<n>,...] row of a ?WDP,DUMP (wdpDump, WCB_WDP.cpp), or None when there is no such row. A
    client's ALIAS is the device type it advertises (WDP_TLV_DEVTYPE, wdpOnAdvertReceived)."""
    row = next((x for x in lines if x.startswith(f"[WDP:N={n},")), None)
    m = re.search(r",ALIAS=(.*?),HW=", row) if row else None
    return m.group(1) if m else None


def _routable(name):
    """An alias ;W<alias>,<cmd> can carry: no comma or space, and not digit-led (a digit-led name parses as a number)."""
    return bool(name) and "," not in name and " " not in name and not name[0].isdigit()


def _c_dump(c):
    """?WDP,DUMP on a Console -> its lines."""
    m = c.send("?WDP,DUMP")
    c.expect(r"^\[WDP:END", timeout=5, since=m)
    return c.lines(m)


def _c_alias_is(c, n, alias, timeout):
    """True once the board behind Console `c` lists WCB<n> under `alias`, within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        if _dump_alias(_c_dump(c), n) == alias:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.5)


@contextmanager
def _client_typed(bench, probe_name, device_id, dev_type):
    """probe_in_mesh, with the probe advertising `dev_type` as its WDP device type. probe_in_mesh has no dev_type
    argument (Probe.mesh_join has, hil/probe.py), so the probe's mesh_join is wrapped for the duration."""
    probe = bench.probe(probe_name)
    join = probe.mesh_join
    probe.mesh_join = lambda *a, **k: join(*a, **{**k, "dev_type": dev_type})
    try:
        with probe_in_mesh(bench, probe_name, device_id) as p:
            yield p
    finally:
        del probe.mesh_join


@test("mesh.alias_case", ";W<alias>,<cmd> matches W2's alias in any letter case", needs=["wcb1", "probe2"])
def alias_case(bench):
    """WCB-WP54 (wdp.alias.ambiguous_case_client, the case half). processWCBMessage (WCB.ino) compares the board's own
    alias with equalsIgnoreCase, and wdpResolveAlias (WCB_WDP.cpp) matches the neighbor table the same way."""
    name = _alias_of(snapshot(bench, 2))
    if not _routable(name):
        raise Skip(f"W2 has no alias that ;W can carry ({name!r})")
    mine = _alias_of(snapshot(bench, 1))
    if mine and mine.lower() == name.lower():
        raise Skip("W1 has the same alias as W2")
    spellings = [s for s in dict.fromkeys((name.lower(), name.upper(), name.swapcase())) if s != name]
    if not spellings:
        raise Skip(f"W2's alias {name!r} has no letters to change the case of")
    probe, ch = wire(bench, 2, "S2")
    w = usb_wcb(bench)
    for s in spellings:
        t = marker()
        m = probe.dev.mark()
        w.send(f";W{s},;S2{t}")
        probe.expect_bytes(ch, t.encode() + b"\r", timeout=3, since=m)


@test("mesh.alias_client_devtype", ";W<NaviCore's device type, case changed>,?version reaches NaviCore: a client is addressed by the device type it advertises", needs=["wcb1", "navicore"], links=[])
def alias_client_devtype(bench):
    """WCB-WP54. A WCB_Client advertises no alias: wdpOnAdvertReceived (WCB_WDP.cpp) stores its WDP_TLV_DEVTYPE as the
    neighbor's alias, and wdpResolveAlias matches it case-insensitively. NaviCore answers a mesh ?version through its
    remote terminal to the sender, ending 'End of Version' (WCBClient WCB_Mgmt.h), which W1 prints as [TERM:20]."""
    w = usb_wcb(bench)
    name = _dump_alias(w.run("?WDP,DUMP", timeout=8), 20)
    if not name:
        w.run("?WDP,POLL")
        time.sleep(2.0)
        name = _dump_alias(w.run("?WDP,DUMP", timeout=8), 20)
    if not _routable(name):
        raise Skip(f"W1 has no usable WDP alias for WCB20, NaviCore ({name!r})")
    spelled = name.lower() if name.lower() != name else name.upper()
    bench.note(f"NaviCore advertises {name!r}; routed as ;W{spelled}")
    m = w.send(f";W{spelled},?version")
    w.dev.expect(r"^\[TERM:20\]End of Version", timeout=5, since=m)


@test("mesh.alias_ambiguous_wcb", "With W1 aliased as NaviCore's device type, W2 refuses ;W<that name> as ambiguous and routes nothing, while W1 takes the name as its own", needs=["wcb1", "navicore"], links=[])
def alias_ambiguous_wcb(bench):
    """WCB-WP54 (wdp.alias.ambiguous_case_client). processWCBMessage (WCB.ino) matches the board's own alias before it
    asks wdpResolveAlias, which skips the local board and returns -1 when two neighbors carry the name (WCB_WDP.cpp):
    here W1 (its WDP_TLV_ALIAS) and NaviCore (its WDP_TLV_DEVTYPE). An alias change is not advertised by itself, so
    ?WDP,POLL on W1 sends it (W1 advertises and solicits); the restore polls again, so W2 and NaviCore relearn W1's
    real alias."""
    w = usb_wcb(bench)
    name = _dump_alias(w.run("?WDP,DUMP", timeout=8), 20)
    if not _routable(name):
        raise Skip(f"W1 has no usable WDP alias for WCB20, NaviCore ({name!r})")
    problems = []
    with config_guard(bench, 1) as before, Console(bench, 2) as c2:
        if _dump_alias(_c_dump(c2), 20) != name:
            raise Skip(f"W2 does not list NaviCore as {name!r}")
        orig = token(before[1], "?ALIAS,")
        try:
            out = w.run(f"?ALIAS,{name}")
            assert any(f"WCB alias set to: {name}" in x for x in out), f"?ALIAS,{name} said {out}"
            w.run("?WDP,POLL")
            assert _c_alias_is(c2, 1, name, 12), "W2 did not learn W1's new alias within 12 s of ?WDP,POLL"
            t = marker()
            m1 = w.dev.mark()
            m = c2.send(f";W{name},;S0{t}")
            try:
                c2.expect(AMBIGUOUS.format(re.escape(name)), timeout=4, since=m)
            except AssertionError:
                problems.append(f"W2 printed no ambiguous line for ;W{name}: {c2.lines(m)[-6:]}")
            time.sleep(1.0)
            if any(x.startswith(t) for x in w.dev.since(m1)):
                problems.append(f"W2 routed ;W{name} to W1 although two boards carry the name")
            # Not WCB.run(): a ;W to the board's own alias re-queues its payload (enqueueCommand), behind run()'s echo.
            t = marker()
            m = w.send(f";W{name},;S0{t}")
            try:
                w.dev.expect(rf"^{t}(?![0-9A-F])", timeout=3, since=m)
            except AssertionError:
                problems.append(f"W1 did not run ;W<its own alias>,;S0<t> itself: {w.dev.since(m)[-4:]}")
            if any(x.startswith("[;w]") for x in w.dev.since(m)):
                problems.append(f"W1 refused its own alias: {[x for x in w.dev.since(m) if x.startswith('[;w]')]}")
        finally:
            w.run(orig if orig else "?ALIAS,CLEAR")
            w.run("?WDP,POLL")
            try:
                if not _c_alias_is(c2, 1, _alias_of([orig] if orig else []) or "", 12):
                    bench.note("W2 still listed W1 under the test alias 12 s after the restoring ?WDP,POLL")
            except AssertionError as e:
                bench.note(f"W2's ?WDP,DUMP after the restore failed: {(str(e).splitlines() or [repr(e)])[0]}")
    assert not problems, "; ".join(problems)


@test("mesh.alias_ambiguous_client", "A temporary client advertising W2's alias as its device type makes ;W<alias> ambiguous on W1: refused, nothing reaches W2 S2; once the client has left and been forgotten, the alias reaches W2 again", needs=["wcb1", "probe1"])
def alias_ambiguous_client(bench):
    """WCB-WP54. wdpResolveAlias (WCB_WDP.cpp) counts every valid neighbor whose alias matches, WCBs and clients alike,
    and returns -1 for more than one; processWCBMessage then prints the ambiguous line and sends nothing. probe1 joins
    as a temporary WCB_Client whose device type is W2's alias (_client_typed). It sends no commands, and adverts are
    decoded before the duplicate ring (espNowReceiveCallback), so the one-id-per-sending-test rule (s19 MESH_IDS) does
    not apply: any id with no row in W1's table will do. probe_in_mesh's ?WDP,FORGET drops the row on the way out
    (wdpForgetNeighbor), which the last check relies on."""
    s2 = link(bench, 2, "S2")
    name = _alias_of(snapshot(bench, 2))
    if not _routable(name):
        raise Skip(f"W2 has no alias that ;W can carry ({name!r})")
    w = usb_wcb(bench)
    dump = w.run("?WDP,DUMP", timeout=8)
    pid = next((p for p in CLIENT_IDS if not any(x.startswith(f"[WDP:N={p},") for x in dump)), None)
    if pid is None:
        raise Skip("every candidate client id already has a row in W1's WDP table")
    problems = []
    with _client_typed(bench, "probe1", pid, name):
        deadline = time.monotonic() + 15
        while _dump_alias(w.run("?WDP,DUMP", timeout=8), pid) != name:
            if time.monotonic() >= deadline:
                raise AssertionError(f"W1 never listed the probe (WCB{pid}) under {name!r} within 15 s")
            time.sleep(1.0)
        t = marker()
        watch = Watch(s2)
        out = w.run(f";W{name},;S2{t}")
        if not any(re.match(AMBIGUOUS.format(re.escape(name)), x) for x in out):
            problems.append(f";W{name} with a WCB and a client of that name printed no ambiguous line: {out}")
        time.sleep(1.5)
        if watch.got(s2):
            problems.append(f"the refused route still reached W2 S2: {watch.got(s2)!r}")
    t = marker()
    watch = Watch(s2)
    w.send(f";W{name},;S2{t}")
    if not _arrived(watch, s2, t.encode() + b"\r", 4):
        problems.append(f"after the client left and was forgotten, ;W{name} no longer reached W2 S2")
    assert not problems, "; ".join(problems)


@test("mesh.w_usage", "A bare ;W (or ;w) and an alias with no comma (;Wdome) each print their usage line", needs=["wcb1"], links=[])
def w_usage(bench):
    """WCB-WP54 (wcb.cmd.w_route_forms). processWCBMessage (WCB.ino): a bare ;w prints '[;w] usage: ...'; the alias
    form needs a comma, and '[;w] alias form: use ...' says so. The two lines differ (the plan calls both 'the usage
    line')."""
    w = usb_wcb(bench)
    bare = "[;w] usage: ;w<target|alias>,<command>"
    alias = "[;w] alias form: use ;w<alias>,<command>  (e.g. ;wdome,;s2test)"
    problems = []
    for cmd, want in ((";W", bare), (";w", bare), (";Wdome", alias)):
        out = [x.rstrip() for x in w.run(cmd)]
        if want not in out:
            problems.append(f"{cmd}: expected {want!r}, got {out}")
    assert not problems, "; ".join(problems)


@test("mesh.w_legacy_digits", "Legacy ;w2<digits>: one digit is the target and every digit after it is the payload, so W2 gets exactly those digits - plain text out of W2 S3, not re-broadcast to W1 S2", needs=["wcb1"])
def w_legacy_digits(bench):
    """WCB-WP54 (legacy.s_baud_and_w_edges, the ;w half). processWCBMessage (WCB.ino): with no comma after the digit run,
    exactly ONE digit is the target and the rest - digits included - is the payload (';w2100' sends '100' to WCB2).
    W2 receives it as plain text over the mesh (origin 1), so it goes out of W2's broadcast ports and no further."""
    far, near = link(bench, 2, "S3"), link(bench, 1, "S2")
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON")
    digits = "".join(secrets.choice("0123456789") for _ in range(12))
    watch = Watch(far, near)
    usb_wcb(bench).send(f";w2{digits}")
    watch.expect(far, digits.encode() + b"\r", timeout=4)
    time.sleep(1.5)
    lines = watch.got(far).split(b"\r")
    assert lines.count(digits.encode()) == 1 and not any(x.endswith(digits.encode()) and x != digits.encode()
                                                         for x in lines), \
        f"W2 S3 got {watch.got(far)!r}, expected the {len(digits)} digits alone, once"
    assert not watch.got(near), f"the digits reached W1 S2: {watch.got(near)!r}"
