"""The commands and behaviours the 2026-09-24 coverage scan found untested (docs/HIL_TEST_AUDIT.md §7, WP10):
the current ?TRACK command, four old spellings (?MAESTROM..., ?M3xx and the short ?M2/?M3 forms, ?ETMCHAR, ?HELP),
the ;T timer's 30-minute cap and a ;T chain received over the mesh, the ?MGMT and ?RTERM error replies, and the boot
banner on both boards, checked line by line against each board's own config chain.

Restore if aborted: ?DEBUG,OFF / ?DEBUG,MGMT,OFF / ?STOP / ?TRACK,ON on W1's USB console; ?MAESTRO,CLEAR,M8:W11S1 if a
chain shows ?MAESTRO,M8:W11S1:57600; ?WDP,ON if the chain shows ?WDP,OFF. Nothing here moves a servo.
"""
import re
import time

from hil.runner import Skip, test
from suites.common import Console, config_guard, marker, require_tokens, snapshot, token, usb_wcb, wire
from suites.s22_maestro_kyber import _wdp_off


def _has(lines, text):
    return any(text in x for x in lines)


def _in_order(lines, wanted, label):
    i = 0
    for n in wanted:
        while i < len(lines) and n not in lines[i]:
            i += 1
        if i == len(lines):
            return [f"{label}: missing '{n}' (in order)"]
        i += 1
    return []


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


# ============================================================ ?TRACK, removed (F5)
@test("misc.track_removed", "?TRACK and its old spellings ?TRACK_ALL_ON / _OFF / ?TRACK_STATUS are gone: each is reported as an unknown command, not silently swallowed", needs=["wcb1"], links=[])
def track_removed(bench):
    """?TRACK set a flag nothing read and no help page listed; it was removed with its legacy spellings on 2026-09-24
    (docs/HIL_TEST_AUDIT.md F5). The legacy dispatcher's last branch names what it did not recognise (WCB.ino)."""
    w = usb_wcb(bench)
    bad = []
    for cmd in ("?TRACK,ON", "?TRACK,OFF", "?TRACK,STATUS", "?TRACK_ALL_ON", "?TRACK_ALL_OFF", "?TRACK_STATUS"):
        out = [x.rstrip() for x in w.run(cmd)]
        if f"Unknown command: {cmd[1:]}" not in out:
            bad.append(f"{cmd} printed {out}")
        if _has(out, "Delivery tracking") or _has(out, "Command delivery tracking"):
            bad.append(f"{cmd} still reached the removed handler")
    assert not bad, "; ".join(bad)


# ============================================================ old spellings
@test("maestro.legacy_no_comma", "Legacy ?MAESTROM<id>:W<wcb>S<port>:<baud> (no comma) adds a Maestro slot like ?MAESTRO,M..., held in the chain in the canonical form; a legacy line not starting with M is refused; cleared after (WDP off)", needs=["wcb1"], links=[])
def maestro_legacy_no_comma(bench):
    """WCB.ino's legacy block hands message.substring(7) to configureMaestro (WCB_Maestro.cpp). The slot is a remote
    placeholder on WCB11, which does not exist, so nothing is ever sent to a real Maestro."""
    w = usb_wcb(bench)
    toks = bench.config_tokens(1, refresh=True)
    if len([t for t in toks if re.match(r"^\?MAESTRO,M\d:", t, re.I)]) >= 9:
        raise Skip("W1's Maestro table is full")
    if any(re.match(r"^\?MAESTRO,M8:W11S", t, re.I) for t in toks):
        raise Skip("W1 already has a Maestro 8 on WCB11")
    problems = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            added = False
            try:
                out = w.run("?MAESTROM8:W11S1:57600")
                added = _has(out, "✓ Maestro 8: Remote on WCB11 (unicast, slot ")
                if not added:
                    problems.append(f"?MAESTROM8:W11S1:57600 printed {out}")
                if "?MAESTRO,M8:W11S1:57600" not in snapshot(bench, 1):
                    problems.append("the chain lacks ?MAESTRO,M8:W11S1:57600")
                out = w.run("?MAESTROX8:W11S1:57600")
                if not (_has(out, "Invalid format. Use: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>")
                        and _has(out, "Example: ?MAESTRO,M2:W2S1:57600")):
                    problems.append(f"?MAESTROX8:... printed {out}")
            finally:
                out = w.run("?MAESTRO,CLEAR,M8:W11S1")
                if added and not _has(out, "Cleared Maestro M8:W11S1 (freed slot "):
                    problems.append(f"clearing the placeholder printed {out}")
    assert not problems, "; ".join(problems)


@test("ident.legacy_mac_short", "Legacy ?M3xx rewrites MAC octet 3 (with its own value, so nothing changes); a non-hex ?M3ZZ and the short ?M2 / ?M3 forms are refused", needs=["wcb1"], links=[])
def legacy_mac_short(bench):
    """update2ndMACOctet / update3rdMACOctet (WCB.ino). ?M2xx with a value is ident.mac_octet2_live; writing octet 3's
    own value back touches NVS but changes nothing, and config_guard proves the chain is unchanged."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        cur = token(before[1], "?MAC,3,")
        if cur is None:
            raise Skip("W1's chain lacks ?MAC,3")
        orig = cur[len("?MAC,3,"):].upper()
        for cmd, want in ((f"?M3{orig}", f"Updated the 3rd Octet to 0x{orig}"),
                          ("?M3ZZ", "Invalid hex value for 3rd MAC octet. Use two hex digits (00-FF)."),
                          ("?M3", "Invalid command. Use ?M3xx where xx is two hex digits."),
                          ("?M2", "Invalid command. Use ?M2xx where xx is two hex digits.")):
            out = [x.rstrip() for x in w.run(cmd)]
            if want not in out:
                problems.append(f"{cmd} printed {out}, expected {want!r}")
    assert not problems, "; ".join(problems)


@test("etm.char_legacy", "Legacy ?ETMCHAR runs the same ETM characterisation as ?ETM,CHAR: all three phases report per-board rows and it ends with a recommended timeout (loads the mesh for a few seconds)", needs=["wcb1"], links=[])
def etm_char_legacy(bench):
    """startETMChar (WCB.ino). The loss thresholds are etm.char_unicast / etm.char_loaded (s99); this checks that the old
    spelling reaches the same run and that it completes."""
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        m = w.send("?ETMCHAR")
        rec = w.dev.expect(r"Recommended ETM timeout: (\d+)ms", timeout=180, since=m)
        time.sleep(1)
        lines = w.dev.since(m)
    phases, rows, phase = set(), 0, None
    for t in lines:
        p = re.match(r"^ Phase (\d) - ", t)
        if p:
            phase = int(p.group(1))
            phases.add(phase)
        if phase and re.search(r"WCB\d+: Min: \d+ms, Max: \d+ms, Avg: \d+ms, Missed: \d+%", t):
            rows += 1
    bench.note(f"?ETMCHAR recommended {rec.group(1)} ms; phases {sorted(phases)}, {rows} result rows")
    assert phases == {1, 2, 3}, f"?ETMCHAR reported phases {sorted(phases)}"
    assert rows >= 3, f"?ETMCHAR printed only {rows} per-board result rows"


@test("wcb.help_forms", "?HELP, ?help and a bare ? all print the top-level command reference", needs=["wcb1"], links=[])
def help_forms(bench):
    """processLocalCommand (WCB.ino): an empty message, '?', 'HELP' or 'help' is printCommandHelp(""). The ?<verb>? form
    is wcb.help_trap."""
    w = usb_wcb(bench)
    head = "Wireless Communication Board (WCB) - Command Reference"
    bad = [cmd for cmd in ("?HELP", "?help", "?") if not _has(w.run(cmd), head)]
    assert not bad, f"no command reference for {bad}"


# ============================================================ ?BAUD and ?LABEL validation
@test("persist.baud_label_validation", "?BAUD and ?LABEL refusals: no rate, an unknown rate, a port outside 1-5, a soft port above 115200; the soft-port warning above 57600, where the rate still applies (put back at once); ?LABEL without text, an unknown form, a bad CLEAR target, and a port outside 1-5, which ?LABEL and the old ?SLCS spelling now name (F7); ?LABEL,CLEAR,ALL clears all five, put back from the baseline", needs=["wcb1"], links=[])
def baud_label_validation(bench):
    """The ?BAUD and ?LABEL handlers (WCB.ino), updateBaudRate and saveSerialLabelToPreferences (WCB_Storage.cpp).
    A port outside 1-5 was ignored with no reply until 2026-09-24 (docs/HIL_TEST_AUDIT.md F7); ?LABEL now answers as
    ?BAUD does, and the old ?SLCSx spelling as ?SLSx does."""
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?BAUD,S5,9600")
    problems = []
    with config_guard(bench, 1) as before:
        labels = [t for t in before[1] if t.upper().startswith("?LABEL,")]
        fmt_baud = "Invalid format. Use: ?BAUD,Sx,rate"
        fmt_clear = "Invalid format. Use: ?LABEL,CLEAR,Sx or ?LABEL,CLEAR,ALL"
        for cmd, want in (("?BAUD", fmt_baud), ("?BAUD,S3", fmt_baud),
                          ("?BAUD,S3,12345", "Invalid baud rate"),
                          ("?BAUD,S6,9600", "Invalid serial port 6 (must be 1-5)"),
                          ("?BAUD,SX,9600", "Invalid serial port 0 (must be 1-5)"),
                          ("?BAUD,S3,128000", "S3 is a software UART - 128000 baud is above the 115200 it supports. Use S1 or S2 for this device."),
                          ("?LABEL,S3", "Invalid format. Use: ?LABEL,Sx,text"),
                          ("?LABEL,FOO", "Invalid LABEL command. Use: ?LABEL ?"),
                          ("?LABEL,CLEAR,X", fmt_clear), ("?LABEL,CLEAR", fmt_clear)):
            out = [x.rstrip() for x in w.run(cmd)]
            if want not in out:
                problems.append(f"{cmd} printed {out}, expected {want!r}")
            if _has(out, "Baud rate for Serial") or _has(out, "label set to"):
                problems.append(f"{cmd} changed a setting")
        for cmd, want in (("?LABEL,S9,HILX", "Invalid serial port 9 (must be 1-5)"),
                          ("?LABEL,CLEAR,S9", "Invalid serial port 9 (must be 1-5)"),
                          ("?LABEL,SX,HILX", "Invalid serial port 0 (must be 1-5)"),
                          ("?SLCS9", "Invalid port. Must be 1-5")):
            out = [x.rstrip() for x in w.run(cmd)]
            if want not in out:
                problems.append(f"{cmd} printed {out}, expected {want!r}")
            if _has(out, "label set to") or _has(out, "label cleared"):
                problems.append(f"{cmd} changed a label")
        warned = False
        try:
            out = [x.rstrip() for x in w.run("?BAUD,S5,115200")]
            warned = True
            if ("Warning: S5 is a software UART - input at 115200 baud is not received reliably (measured exact through "
                    "57600, none at 115200). Output is fine.") not in out:
                problems.append(f"?BAUD,S5,115200 printed no soft-port warning: {out}")
            if "Baud rate for Serial5 updated to 115200" not in out:
                problems.append("?BAUD,S5,115200 did not apply the rate after its warning")
        finally:
            if warned:
                w.run("?BAUD,S5,9600")
        cleared = False
        try:
            out = [x.rstrip() for x in w.run("?LABEL,CLEAR,ALL")]
            cleared = True
            problems += _in_order(out, [f"Serial{k} label cleared" for k in range(1, 6)] + ["All serial labels cleared"], "?LABEL,CLEAR,ALL")
            left = [t for t in snapshot(bench, 1) if t.upper().startswith("?LABEL,")]
            if left:
                problems.append(f"labels left after ?LABEL,CLEAR,ALL: {left}")
        finally:
            if cleared:
                for t in labels:
                    w.run(t)
    assert not problems, "; ".join(problems)


# ============================================================ timers
@test("serial.timer_cap_negative", "A ;T delay above 30 minutes is capped at 1800000 ms and says so, with ?DEBUG on or off (the Warning line only with it); exactly 1800000 is not capped; a negative delay refuses the whole chain, so nothing in it runs, in either ;T spelling (F6); each capped sequence is stopped before its second command", needs=["wcb1"], links=[])
def timer_cap_negative(bench):
    """parseCommandGroups (command_timer.cpp). Until 2026-09-24 the delay was String::toInt() stored in an unsigned
    long, so -1 wrapped to 4294967295 and was capped to 30 minutes instead of refused, and the cap was reported only
    under ?DEBUG (docs/HIL_TEST_AUDIT.md F6). The first group (;S0 to USB) shows whether a chain started."""
    w = usb_wcb(bench)
    problems = []
    cap = "Delay capped to 1800000 ms : originally requested {} ms"
    warn = "Delay exceeds configured limits of 1800000. Input: {} ms"

    def chain(delay, debug):
        a, b = marker("a"), marker("b")
        m = w.send(f";S0{a}^;T{delay}^;S0{b}")
        time.sleep(1.5)
        out = [x.rstrip() for x in w.dev.since(m)]
        w.run("?STOP")
        return a, b, out

    with config_guard(bench, 1):
        try:
            for debug in (False, True):
                w.run("?DEBUG,ON" if debug else "?DEBUG,OFF")
                tag = "debug on" if debug else "debug off"
                a, b, out = chain("1800001", debug)
                if a not in out:
                    problems.append(f";T1800001 ({tag}): the first group did not run")
                if not _has(out, cap.format("1800001")):
                    problems.append(f";T1800001 ({tag}): no cap line")
                if _has(out, warn.format("1800001")) != debug:
                    problems.append(f";T1800001 ({tag}): the Warning line {'missing' if debug else 'printed without debug'}")
                if b in out:
                    problems.append(f";T1800001 ({tag}): the second command ran")
                a, b, out = chain("1800000", debug)
                if a not in out or _has(out, "Delay capped") or _has(out, "Delay exceeds"):
                    problems.append(f";T1800000 ({tag}): the first group did not run, or the delay was reported capped")
            w.run("?DEBUG,OFF")
            for spell in ("-1", "-500"):
                a, b, out = chain(spell, False)
                if not _has(out, f"Timer refused: ';T{spell}' is a negative delay. Nothing in this chain was run."):
                    problems.append(f";T{spell}: no refusal line in {out[-4:]}")
                if a in out or b in out:
                    problems.append(f";T{spell}: part of the refused chain ran")
            c = marker("c")
            m = w.send(f";t-500,;S0{c}")                  # the comma spelling carries its command in the same token
            time.sleep(1.5)
            out = [x.rstrip() for x in w.dev.since(m)]
            if not _has(out, "Timer refused: ';t-500,;S0") or c in out:
                problems.append(f";t-500,<cmd>: not refused cleanly: {out[-4:]}")
        finally:
            w.run("?STOP")
            w.run("?DEBUG,OFF")
    assert not problems, "; ".join(problems)


@test("mesh.timer_chain", "A ;T chain delivered to W2 by ?MGMT,FRAG keeps its delay on W2: the second command reaches W2 S2 about 800 ms after the first", needs=["wcb1"], links=["W2S2"])
def mesh_timer_chain(bench):
    """A plain ;W2,a^b line sends only 'a' (the sender splits the chain), so a remote chain rides ?MGMT,FRAG
    (mesh.frag_chain); this adds a timer to it. W2 parses the groups itself, so the gap is timed on W2's own port."""
    probe, ch = wire(bench, 2, "S2")
    a, b = marker("a"), marker("b")
    m = probe.dev.mark()
    usb_wcb(bench).send(f"?MGMT,FRAG,2,{marker()[3:7]},0,1,;S2{a}^;T800^;S2{b}")
    probe.expect_bytes(ch, b.encode(), timeout=5, since=m)
    gap = probe.time_of(ch, b.encode(), m) - probe.time_of(ch, a.encode(), m)
    bench.note(f"timer gap on W2: {gap} ms")
    assert 700 <= gap <= 1100, f"gap {gap} ms, expected ~800"


# ============================================================ ?MGMT and ?RTERM error replies
@test("mesh.mgmt_errors", "?MGMT error replies: the oversize-fragment refusal prints with debug off; with ?DEBUG,MGMT on, a bare ?MGMT, an unknown sub-command, the ETM forms, a malformed or out-of-range FRAG and an out-of-range PULL each say why; STATS and ETM,CHAR to WCB 0 are dropped silently", needs=["wcb1"], links=[])
def mgmt_errors(bench):
    """handleMgmtForward and the request handlers (WCB.ino). Every reply but the oversize one is behind debugMGMT, so
    without MGMT debug a relay that gets a bad line says nothing at all. The SEQ / SEQGET errors are inv.*. No line here
    reaches another board: each one is refused before anything is sent."""
    w = usb_wcb(bench)
    sid = marker()[3:7]
    problems = []
    with config_guard(bench, 1):
        out = w.run(f"?MGMT,FRAG,2,{sid},0,2,{'x' * 180}")
        if not _has(out, "[MGMT] FRAG payload 180 > 179 chars — REJECTED (sender must fragment at 179)"):
            problems.append(f"the 180-char fragment printed {out}")
        if [x for x in w.run("?MGMT,BOGUS,1") if x.startswith("[MGMT]")]:
            problems.append("an unknown sub-command printed a reply with MGMT debug off")
        try:
            if not _has(w.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled"):
                problems.append("?DEBUG,MGMT,ON did not confirm")
            checks = [
                ("?MGMT", "[MGMT] Invalid format"),
                ("?MGMT,BOGUS,1", "[MGMT] Unknown MGMT subcommand"),
                ("?MGMT,ETM,CHAR", "[MGMT] ETM: expected ?MGMT,ETM,CHAR,<target>"),
                ("?MGMT,ETM,FOO,2", "[MGMT] ETM: unknown subcommand 'FOO'"),
                (f"?MGMT,FRAG,2,{sid}", "[MGMT] Malformed FRAG — expected FRAG,target,sessionId,chunkIdx,total,payload"),
                (f"?MGMT,FRAG,21,{sid},0,1,x", "[MGMT] Invalid parameters"),
                (f"?MGMT,FRAG,0,{sid},0,1,x", "[MGMT] Invalid parameters"),
                (f"?MGMT,FRAG,2,{sid},0,0,x", "[MGMT] Invalid parameters"),
                (f"?MGMT,FRAG,2,{sid},0,17,x", "[MGMT] Invalid parameters"),
                ("?MGMT,PULL,0", "[MGMT] PULL: invalid targetWCB"),
                ("?MGMT,PULL,21", "[MGMT] PULL: invalid targetWCB"),
            ]
            for cmd, want in checks:
                out = w.run(cmd)
                if not _has(out, want):
                    problems.append(f"{cmd} printed {out}, expected {want!r}")
            for cmd, sent in (("?MGMT,STATS,0", "[MGMT] Stats request sent"), ("?MGMT,ETM,CHAR,0", "[MGMT] ETM char request sent")):
                out = w.run(cmd)
                if [x for x in out if x.startswith("[MGMT]")]:
                    problems.append(f"{cmd} printed {out}, expected nothing")
        finally:
            w.run("?DEBUG,MGMT,OFF")
    assert not problems, "; ".join(problems)


@test("mesh.rterm_errors", "?RTERM error replies: START with a relay outside 1-20 or not a number, an unknown or missing sub-command, and STOP with no session (which still confirms)", needs=["wcb1"], links=[])
def rterm_errors(bench):
    """The ?RTERM handler (WCB.ino) and WCBSerial::stopSession (WCB_RemoteTerm.cpp). A valid START is mesh.rterm and
    every Console session; nothing here starts one."""
    w = usb_wcb(bench)
    relay = "[RTERM] Invalid relay WCB. Use ?RTERM,START,1..20"
    unknown = "[RTERM] Unknown sub-command. Use: ?RTERM,START,<n> or ?RTERM,STOP"
    problems = []
    for cmd, want in (("?RTERM,START,0", relay), ("?RTERM,START,21", relay), ("?RTERM,START,X", relay),
                      ("?RTERM,FOO", unknown), ("?RTERM", unknown), ("?RTERM,START", unknown),
                      ("?RTERM,STOP", "[RTERM] Session stopped")):
        out = [x.rstrip() for x in w.run(cmd)]
        if want not in out:
            problems.append(f"{cmd} printed {out}, expected {want!r}")
        if _has(out, "[RTERM] Session started"):
            problems.append(f"{cmd} started a session")
    assert not problems, "; ".join(problems)


# ============================================================ boot banner
HW_NAMES = {1: "1.0", 21: "2.1", 23: "2.3", 24: "2.4", 31: "3.1", 32: "3.2"}


def _live_peers(lines):
    for x in lines:
        m = re.match(r"^Live peers: (\d+) \(WCBQ floor (\d+)", x)
        if m:
            return int(m.group(1))
    return None


def _banner_problems(banner, toks, version, live):
    """Every banner line that follows from the config chain `toks` (setup() in WCB.ino, the load* functions in
    WCB_Storage.cpp and the subsystem files). Credentials in the chain are never read: the ?WIFI token is used for its
    mode and SSID only, and ?EPASS not at all."""
    p = []

    def num(prefix):
        t = token(toks, prefix)
        return int(t[len(prefix):].split(",")[0]) if t else None

    n, q, hw = num("?WCB,"), num("?WCBQ,"), num("?HW,")
    ch = num("?WCBCH,")
    o2 = (token(toks, "?MAC,2,") or "?MAC,2,??")[len("?MAC,2,"):].upper()
    o3 = (token(toks, "?MAC,3,") or "?MAC,3,??")[len("?MAC,3,"):].upper()
    if None in (n, q, ch):
        return ["the chain lacks ?WCB, ?WCBQ or ?WCBCH"]
    exact = ["ETM struct size: 252", "Normal struct size: 249",           # CLAUDE.md rule 4
             f"Booting up the Wireless Communication Board {n} (W{n})",
             f"Software Version: {version}", f"Number of WCBs in the system: {q}",
             f"ESP-NOW MAC Address: 02:{o2}:{o3}:00:00:{n:02X}",
             f"Added ESP-NOW broadcast peer: FF:{o2}:{o3}:FF:FF:FF",
             "Reset reason: 3 - Software Reset",                         # a ?reboot is a software reset
             "Raw Serial Forwarding Task Created"]
    if hw in HW_NAMES:
        exact.append(f"HW Version: {HW_NAMES[hw]}")
    exact += [f"Added ESP-NOW peer: 02:{o2}:{o3}:00:00:{k:02X}" for k in range(1, q + 1) if k != n]
    c = token(toks, "?CONTROLLER,ON,")
    if c:
        cid = int(c.split(",")[2])
        exact.append(f"Added ESP-NOW special peer WCB{cid}: 02:{o2}:{o3}:00:00:{cid:02X}")
    d = token(toks, "?DELIM,")
    f = token(toks, "?FUNCCHAR,")
    cc = token(toks, "?CMDCHAR,")
    exact += [f"Delimeter Character: {d[-1] if d else '^'}",           # (sic) the firmware's spelling
              f"Local Function Identifier: {f[-1] if f else '?'}",
              f"Command Character: {cc[-1] if cc else ';'}"]
    if "?ETM,ON" in toks:
        exact.append("ETM: ENABLED")
    wdp_on = "?WDP,OFF" not in toks
    if wdp_on:
        exact.append("[WDP] discovery enabled")
    elif _has(banner, "[WDP] discovery enabled"):
        p.append("WDP is off in the chain, yet the banner says discovery enabled")
    a = token(toks, "?ALIAS,")
    if a:
        exact.append(f"Loaded WCB alias: {a[len('?ALIAS,'):]}")
    r = token(toks, "?HCR,REMOTE,W")
    if r:
        exact.append(f"[HCR] Loaded: routes ;H to WCB{int(r[len('?HCR,REMOTE,W'):])}")
    hp = token(toks, "?HCR,PORT,S")
    if hp:
        mm = re.match(r"^\?HCR,PORT,S(\d):(\d+)$", hp)
        poll = num("?HCR,POLL,")
        if mm and poll is not None:
            exact.append(f"[HCR] Loaded: S{mm.group(1)} at {mm.group(2)} baud, poll={poll}s")
    starts = []
    for kind in ("MP3", "DFP"):
        t = next((x for x in toks if re.match(rf"^\?{kind},S\d:\d+:V\d+$", x, re.I)), None)
        if t:
            mm = re.match(rf"^\?{kind},S(\d):(\d+):V(\d+)$", t, re.I)
            starts.append(f"[{kind}] Loaded: S{mm.group(1)} at {mm.group(2)} baud  default vol={mm.group(3)}")
    for t in toks:
        mm = re.match(r"^\?WLED,(\d+):W(\d+)S(\d):(\d+)$", t, re.I)
        if mm:
            wid, host, port, baud = (int(v) for v in mm.groups())
            exact.append(f"[WLED] Loaded: WLED {wid} -> local S{port} @ {baud} baud" if host == n
                         else f"[WLED] Loaded: WLED {wid} -> remote WCB{host} @ {baud} baud")
    nvar = len([t for t in toks if t.upper().startswith("?VAR,SET,")])
    if nvar:
        exact.append(f"[VAR] Loaded {nvar} variable(s)")
    if not any(t.upper().startswith("?MAP,SERIAL,") for t in toks):
        exact.append("  No serial mappings configured")
    if not any(t.upper().startswith("?MAP,PWM") for t in toks):
        exact.append("No input mappings configured")
    maestros = [tuple(int(v) for v in mm.groups()) for t in toks
                for mm in [re.match(r"^\?MAESTRO,M(\d):W(\d+)S(\d):(\d+)$", t, re.I)] if mm]
    remote = "?MAESTRO,REMOTE" in toks or "?KYBER,REMOTE" in toks
    local_kyber = any(t.upper().startswith("?KYBER,LOCAL") for t in toks)
    exact.append(f"Kyber is {'Remote' if remote else 'Local' if local_kyber else 'Not used'}")
    if remote:
        ports = sorted({port for mid, host, port, _ in maestros if host == n})
        exact += [f"Maestro data from the mesh goes to: "
                  + (", ".join(f"S{x}" for x in ports) if ports else "S1 (no local Maestro configured - legacy default)"),
                  "Maestro_Remote Task Created"]
    wifi = token(toks, "?WIFI,")
    mode = wifi.split(",")[1].upper() if wifi else "OFF"
    ssid = wifi.split(",")[2] if wifi and len(wifi.split(",")) > 2 else ""
    if mode == "AP":
        starts.append(f'[WIFI] SoftAP "{ssid}" up on channel {ch} — ws://' if ssid else "[WIFI] SoftAP ")
        exact.append("[WIFI] ESP-NOW shares this channel (WIFI_AP_STA).")
    elif mode == "JOIN":
        starts.append(f'[WIFI] will join "{ssid}" on channel {ch}')
    elif _has(banner, "[WIFI] SoftAP"):
        p.append("WiFi is off in the chain, yet the banner brought a SoftAP up")

    p += [f"no banner line {x!r}" for x in exact if x not in banner]
    p += [f"no banner line starting {x!r}" for x in starts if not any(y.startswith(x) for y in banner)]
    if not any(re.match(r"^WcbCmd Library: \d+\.\d+\.\d+$", x) for x in banner):
        p.append("no 'WcbCmd Library: x.y.z' line")
    # the serial ports: baud, broadcast flags, and the user label in brackets
    for port in range(1, 6):
        b = num(f"?BAUD,S{port},")
        fin = token(toks, f"?BCAST,IN,S{port},")
        fout = token(toks, f"?BCAST,OUT,S{port},")
        if b is None or fin is None or fout is None:
            continue
        head = (f"  Serial{port} Baud: {b}, Broadcast Input: {'Enabled' if fin.upper().endswith(',ON') else 'Disabled'}, "
                f"Broadcast Output: {'Enabled' if fout.upper().endswith(',ON') else 'Disabled'}  Pins: Tx:")
        row = next((x for x in banner if x.startswith(f"  Serial{port} Baud: ")), None)
        lab = token(toks, f"?LABEL,S{port},")
        if row is None or not row.startswith(head):
            p.append(f"S{port} row {row!r}, expected it to start {head!r}")
        elif lab and not row.endswith(f" ({lab.split(',', 2)[2]})"):
            p.append(f"S{port} row {row!r} does not end with its label")
    # the Maestro table, in slot (= backup) order
    rows = [x for x in banner if x.startswith("  Maestro ") and " → " in x]
    want = [(f"  Maestro {mid} → Local S{port}" if host == n else f"  Maestro {mid} → WCB{host}")
            for mid, host, port, _ in maestros]
    if len(rows) != len(want) or any(not (r == w_ or r.startswith(w_ + " S")) for r, w_ in zip(rows, want)):
        p.append(f"Maestro rows {rows}, expected {want}")
    # learned peers: one registration line each, a matching restore count, and the live peer count agrees
    learned = [x for x in banner if re.match(r"^\[PEER\] WCB\d+ registered \(live, learned\)\.$", x)]
    if learned and f"[PEER] restored {len(learned)} learned peer(s) from NVS" not in banner:
        p.append(f"{len(learned)} learned registrations but no matching restore line")
    if live is not None and live != q - 1 + len(learned):
        p.append(f"?PEERSLIVE says {live} after boot; the floor ({q}, minus self) and {len(learned)} learned peers make {q - 1 + len(learned)}")
    p += _in_order(banner, [f"Booting up the Wireless Communication Board {n}", "HW Version: ", "Software Version: ",
                            "WcbCmd Library: ", "Number of WCBs in the system: "], "identity block")
    p += _in_order(banner, [f"  Serial{k} Baud: " for k in range(1, 6)], "port rows")
    p += _in_order(banner, ["ESP-NOW MAC Address: ", "Added ESP-NOW broadcast peer: "], "ESP-NOW block")
    p += _in_order(banner, ["--------Kyber Settings", "------- Maestro Settings"], "Kyber / Maestro block")
    if banner and banner[-1] != "Raw Serial Forwarding Task Created":
        p.append(f"the banner does not end with the forwarding task line: {banner[-1]!r}")
    return p


def _cut(lines):
    """The boot output alone: from the ROM's first line to the forwarding-task line. A line naming a password is
    dropped here, so no check and no failure message can ever carry it."""
    lines = [x.rstrip() for x in lines if "password" not in x.lower()]
    s = next((i for i, x in enumerate(lines) if x.startswith("ets ") or x.startswith("rst:")), 0)
    e = next((i for i, x in enumerate(lines) if x == "Raw Serial Forwarding Task Created" and i >= s), len(lines) - 1)
    return lines[s:e + 1]


@test("boot.banner_w1", "After a reboot, W1's boot banner matches its config chain line by line: identity block, port rows with baud, flags and labels, MAC and peers, controller, WiFi, general settings, loaded devices and routes, Kyber mode, the Maestro table in slot order, learned peers against ?PEERSLIVE (1 reboot)", needs=["wcb1"], links=[])
def banner_w1(bench):
    w = usb_wcb(bench)
    toks = snapshot(bench, 1)
    m = w.reboot()
    banner = _cut(w.dev.since(m))
    version = w.version()
    live = _live_peers(w.run("?PEERSLIVE"))
    problems = _banner_problems(banner, toks, version, live)
    assert not problems, "; ".join(problems)


@test("boot.banner_w2", "After a reboot, W2's boot banner matches its config chain line by line (its own USB console; same checks as boot.banner_w1, here with a local Maestro, a local WLED and stored variables) (1 W2 reboot)", needs=["wcb1"], links=[])
def banner_w2(bench):
    w = usb_wcb(bench)
    toks = snapshot(bench, 2)
    with Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its boot lines cannot be read")
        bm, wm = c2.mark(), w.dev.mark()
        w.send(";W2,?reboot")
        c2.expect(r"Raw Serial Forwarding Task Created", timeout=40, since=bm)
        banner = _cut(c2.lines(bm))
        time.sleep(2.0)                                   # the USB reader's start delay
        ver = next((x.split(": ", 1)[1] for x in _crun(c2, "?VERSION", 1.0) if x.startswith("Software Version: ")), "?")
        live = _live_peers(_crun(c2, "?PEERSLIVE", 1.0))
    problems = _banner_problems(banner, toks, ver, live)
    try:
        w.dev.expect(r"^\[ETM\] WCB2 came ONLINE", timeout=20, since=wm)     # W1 sees it back before the next test
    except AssertionError:
        problems.append("W1 did not see W2 come back online within 20 s of its reboot")
    assert not problems, "; ".join(problems)
