"""Maestro routing and configuration beyond s08's frames, and the Kyber bridge: get-query decoding, fallbacks,
the slot table, every CLEAR form, and ?KYBER LOCAL/REMOTE/CLEAR with their reader tasks.

Built from the maestro_kyber specs, re-verified against the code (several map literals were wrong). Rules:
- Both WCBs are Maestro_Remote, so every byte W1 S1 receives outside a get-query is bridged as a non-ETM Kyber
  broadcast into W2's REAL Maestro 2 (WCB.ino:4828-4850 via KyberRemoteTask, WCB.ino:4568-4580 on W2). Injections
  stay below 0x80 or are device-2 getErrors frames, and tests end with ;M2,getErrors to clear its error bits.
- Nothing here adds a local Maestro id while WDP is on. Remote proxies are never advertised (only slots with
  remoteWCB==0 are, WCB_WDP.cpp:140-167), and ?WDP,OFF stops both adverts (WCB_WDP.cpp:356) and advert handling
  (WCB_WDP.cpp:488), so a slot table can be torn down and rebuilt in backup order without W2 or NaviCore racing a
  proxy into a freed slot. config_guard compares ordered tokens, and backup order is slot order (WCB_Maestro.cpp:936).
- Never a legacy '?MAESTRO_CLEAR…' or '?MAESTRO_DEFAULT' outside a guarded rebuild: the prefix match wipes every slot
  (WCB.ino:5796-5799).
- The Kyber tasks are created only at boot (WCB.ino:8177-8184), so a mode change is followed by ?reboot and every
  restore is ?KYBER,CLEAR + ?MAESTRO,REMOTE + the port's saved ?BAUD/?BCAST tokens + ?reboot.
- The '0'/'9' targets and fallback broadcasts reach real Maestros: those arms use stopScript/goHome/getErrors, and
  subroutine arms run only with bench.json "maestro_safe_sub".
- Probe mesh ids: every id is already used by a command-sending s19 test, and a WCB's duplicate ring (8 seqs per
  sender, WCB.ino:591) is cleared only by a boot announce. _burn_ring sends 17 no-op commands first, which always
  pushes a stale ring out, so a reused id cannot silently drop the test's own commands.
"""
import json
import re
import time
from contextlib import contextmanager, nullcontext

from hil.runner import Skip, test
from suites.common import (Watch, config_guard, link, marker, nonce, padded, probe_in_mesh, require_tokens, snapshot,
                           token, usb_wcb)

NEEDS = ["wcb1", "probe1"]

# configureMaestro's accepted bauds (WCB_Maestro.cpp:610-614) — a backup line outside this set cannot be replayed.
MAESTRO_BAUDS = {0, 110, 300, 600, 1200, 2400, 9600, 14400, 19200, 38400, 57600, 115200, 128000, 256000}
GET_ERRORS_2 = bytes.fromhex("AA0221")


def _has(lines, text):
    return any(text in x for x in lines)


def _in_order(lines, needles, label):
    """[] if every needle appears as a substring, in order, across `lines`; else one problem string."""
    i = 0
    for n in needles:
        while i < len(lines) and n not in lines[i]:
            i += 1
        if i == len(lines):
            return [f"{label}: missing '{n}' (in order) — got {lines}"]
        i += 1
    return []


def _get(w, name):
    return next((x for x in w.run(f"?VAR,GET,{name}") if x.startswith("[VAR] ")), None)


def _not_set(name):
    return f"[VAR] '{name}' is not set (reads as 0)"


def _m_lines(tokens):
    """The ?MAESTRO slot lines of a config chain, in slot order."""
    return [t for t in tokens if re.match(r"^\?MAESTRO,M\d:W\d+S\d:\d+$", t, re.I)]


def _slot(lines, mid, wcb):
    """(port, baud) of the first backup slot for Maestro <mid> on W<wcb>, or None."""
    for t in lines:
        m = re.match(rf"^\?MAESTRO,M{mid}:W{wcb}S(\d):(\d+)$", t, re.I)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _require_remote_pair(bench):
    """W1 and W2 are both Maestro_Remote with a local Maestro on S1 (M1 on W1, M2 on W2). Returns (w1 lines, w2 lines)."""
    require_tokens(bench, 1, "?MAESTRO,REMOTE")
    require_tokens(bench, 2, "?MAESTRO,REMOTE")
    l1, l2 = _m_lines(snapshot(bench, 1)), _m_lines(snapshot(bench, 2))
    if not _slot(l1, 1, 1) or _slot(l1, 1, 1)[0] != 1:
        raise Skip("W1 has no local Maestro 1 on S1")
    if not _slot(l2, 2, 2) or _slot(l2, 2, 2)[0] != 1:
        raise Skip("W2 has no local Maestro 2 on S1")
    return l1, l2


def _require_replayable(lines):
    odd = [t for t in lines if int(t.rsplit(":", 1)[1]) not in MAESTRO_BAUDS]
    if odd:
        raise Skip(f"backup Maestro lines with a baud ?MAESTRO would refuse, so the table could not be rebuilt: {odd}")


def _rebuild(w, lines):
    """?MAESTRO,CLEAR,ALL, then re-add the recorded lines in backup order. Only call with WDP off."""
    problems = []
    w.run("?MAESTRO,CLEAR,ALL", timeout=8)
    for t in lines:
        out = w.run(t)
        if not any(re.search(r"Maestro \d: (Local S\d at \d+ baud|Remote on WCB\d+ \(unicast,) ?\(?slot \d\)", x) for x in out):
            problems.append(f"re-adding {t} printed {out}")
    return problems


@contextmanager
def _wdp_off(w, tokens):
    if "?WDP,OFF" in tokens:
        raise Skip("W1's WDP is already off, so this test could not tell its own ?WDP,OFF apart from the bench's")
    if not _has(w.run("?WDP,OFF"), "[WDP] disabled"):
        raise AssertionError("?WDP,OFF did not confirm")
    try:
        yield
    finally:
        w.run("?WDP,ON")


def _kyber_list(w):
    return [x.rstrip() for x in w.run("?KYBER,LIST")]


def _require_kyber_broadcast_remote(w):
    lst = _kyber_list(w)
    if "Kyber is Remote" not in lst or "Targeting mode: Disabled (Broadcast Mode)" not in lst:
        raise Skip(f"W1 is not Kyber Remote in broadcast mode: {lst}")


def _port_tokens(tokens, port):
    """The saved ?BAUD / ?BCAST tokens of one port — what ?KYBER,CLEAR and a Maestro clear reset to defaults."""
    return [t for t in tokens if re.match(rf"^\?(BAUD,{port},|BCAST,(IN|OUT),{port},)", t, re.I)]


def _port_devices(tokens, port, wcb=1):
    """The saved lines that put a device on W<wcb> <port> ('S2'): a local Maestro, HCR, MP3, DFPlayer, WLED or a PWM
    input/output — what kyberLocalPortRefused (WCB_Storage.cpp) checks. Matched by line prefix, never by scanning every
    token for the port name: a sequence body or the alias can hold 'S2' too, and so can ?EPASS, which must never reach a
    note or a failure message. A mapping's local outputs count too: isSerialPortPWMOutput (WCB_PWM.cpp) reads them, and
    the backup writes them on the ?MAP,PWM,S<in>,... line as S<n> or W<self>S<n>, with no ?MAP,PWM,OUT line."""
    pats = (rf"^\?MAESTRO,M\d:W{wcb}{port}:", rf"^\?HCR,PORT,{port}\b", rf"^\?MP3,{port}\b", rf"^\?DFP,{port}\b",
            rf"^\?WLED,\d+:W{wcb}{port}\b", rf"^\?MAP,PWM,(OUT,)?{port}\b", rf"^\?MAP,PWM,S\d.*,(W{wcb})?{port}(,|$)")
    return [t for t in tokens if any(re.match(p, t, re.I) for p in pats)]


def _raw_stats(w):
    """(attempts, success, failed) of 'Raw Data (Kyber Bridging):' in ?STATS (WCB.ino:1798-1802); zeros when absent."""
    lines = w.run("?STATS")
    for i, x in enumerate(lines):
        if x.startswith("Raw Data (Kyber Bridging):") and i + 1 < len(lines):
            m = re.search(r"Attempts: (\d+), Success: (\d+), Failed: (\d+)", lines[i + 1])
            if m:
                return tuple(int(v) for v in m.groups())
    return 0, 0, 0


def _sent_seq(lines, text):
    """Sequence numbers of '[ETM] Sent seq N: <text>' lines (WCB.ino:2605)."""
    pat = re.compile(rf"^\[ETM\] Sent seq (\d+): {re.escape(text)}(?:\|[0-9A-Fa-f]+)?$")
    return [int(m.group(1)) for m in (pat.match(x) for x in lines) if m]


def _acked_by(lines, wcb, seq):
    return f"[ETM] ACK received from WCB{wcb} for seq {seq}" in lines


def _settle_maestro2(w):
    """Read (and so clear) Maestro 2's error flags after stray data bytes reached it through the bridge."""
    w.send(";M2,getErrors")
    time.sleep(1.5)
    w.run("?VAR,CLEAR,m2err")


def _navicore_hosted(bench, w1_lines):
    """Maestro ids NaviCore may host: W1's WCB20 proxies, plus NaviCore's own GET_CONFIG when it is on USB.
    GET_CONFIG also carries the mesh password — only the maestros list is read, nothing is logged."""
    ids = {int(m.group(1)) for m in (re.match(r"^\?MAESTRO,M(\d):W20S", t, re.I) for t in w1_lines) if m}
    try:
        from hil.navicore import NaviCore
        nc = NaviCore(bench.dev("navicore"))
        got = nc.json_cmd({"type": "GET_CONFIG"}, r'^\{"type":"CONFIG","data":', timeout=10)
        ids |= {s.get("device") for s in json.loads(got.string)["data"].get("maestros", []) if s.get("type") == 1}
    except Exception:  # noqa: BLE001 — NaviCore off USB: the proxies are the best available answer
        pass
    return ids


def _burn_ring(w, probe, target=1):
    """Push a stale duplicate ring for this probe id out of W<target> (see the module docstring)."""
    tag = marker("BURN")
    for i in range(17):
        probe.mesh_send(target, f";S0,{tag}{i:02d}")
    ready = marker("RDY")
    m = w.dev.mark()
    probe.mesh_send(target, f";S0,{ready}")
    w.dev.expect(rf"^{ready}$", timeout=5, since=m)


def _wait_rx(probe, since, sender, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while True:
        hit = next((x for s, x in probe.mesh_received(since) if s == sender and predicate(x)), None)
        if hit is not None or time.monotonic() >= deadline:
            return hit
        time.sleep(0.1)


def _exact(l, since, expected, settle=0.6, timeout=2.0):
    """None if <l> received exactly `expected` after `since`; otherwise a problem string."""
    try:
        l.expect(expected, timeout=timeout, since=since)
    except AssertionError:
        pass
    time.sleep(settle)
    got = l.received(since)
    return None if got == expected else f"{l.key}: expected exactly {expected.hex(' ')}, got {got.hex(' ') or 'nothing'}"


def _restore_remote(w, port_tokens):
    """Undo any ?KYBER mode change on W1: clear, back to Maestro_Remote, the port's saved tokens, reboot."""
    w.run("?KYBER,CLEAR", timeout=6)
    w.run("?MAESTRO,REMOTE", timeout=6)
    for t in port_tokens:
        w.run(t)
    bm = w.reboot()
    if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
        raise AssertionError("W1 did not boot with the Maestro_Remote task after the restore")
    w.run("?DEBUG,OFF")
    _settle_maestro2(w)


# ============================================================ listings (read-only)
@test("maestro.list_and_legacy_spellings", "?MAESTRO,LIST in four spellings follows the backup slot order; bare ?MAESTRO; ?KYBER,LIST = ?KYBER_LIST; bad ?KYBER forms (read-only)", needs=["wcb1"], links=[])
def list_and_legacy_spellings(bench):
    w = usb_wcb(bench)
    me = str(bench.usb_wcb_number())
    bad = []

    def block(cmd):
        out = [x.rstrip() for x in w.run(cmd)]
        i = next((k for k, x in enumerate(out) if x.startswith("------- Maestro Settings ---")), None)
        if i is None:
            bad.append(f"{cmd}: no Maestro Settings header in {out}")
            return []
        return [x for x in out[i + 1:] if x.startswith("  Maestro ") or x == "  No Maestros configured"]

    ref = block("?MAESTRO,LIST")
    for cmd in ("?maestro,list", "?MAESTRO_LIST", "?maestro_list"):   # WCB.ino:5458-5460, 5794-5795
        got = block(cmd)
        if got != ref:
            bad.append(f"{cmd} listed {got}, ?MAESTRO,LIST listed {ref}")
    lines = _m_lines(snapshot(bench, 1))
    # A remote line gains ' S<p>' only when an enabled Kyber target has that id (WCB_Storage.cpp printMaestroSettings).
    want = [rf"^  Maestro {mid} → Local S{port}$" if wcb == me else rf"^  Maestro {mid} → WCB{wcb}( S\d)?$"
            for mid, wcb, port in (re.match(r"^\?MAESTRO,M(\d):W(\d+)S(\d):", t, re.I).groups() for t in lines)]
    ok = (len(ref) == len(want) and all(re.match(p, x) for p, x in zip(want, ref))) if lines else ref == ["  No Maestros configured"]
    if not ok:
        bad.append(f"LIST {ref} does not follow the backup slot order {lines}")
    bad += _in_order(w.run("?MAESTRO"), ["Invalid format. Use: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>",
                                         "Example: ?MAESTRO,M2:W2S1:57600"], "bare ?MAESTRO")
    k1 = [x for x in _kyber_list(w) if x.strip()]
    k2 = [x.rstrip() for x in w.run("?KYBER_LIST") if x.strip()]
    if not k1 or k1[0] != "--- Kyber Configuration ---":
        bad.append(f"?KYBER,LIST printed {k1}")
    if k1 != k2:
        bad.append(f"?KYBER_LIST {k2} differs from ?KYBER,LIST {k1}")
    bench.note("W1 Kyber: " + " / ".join(k1[1:3]))
    for cmd in ("?KYBER", "?KYBER,FOO"):                          # WCB.ino:5181-5190
        if not _has(w.run(cmd), "Invalid KYBER command. Use: ?KYBER ?"):
            bad.append(f"{cmd} was not refused")
    assert not bad, "; ".join(bad)


# ============================================================ get-queries on a local Maestro
@test("maestro.get_local_replies", "W1 S1 get-queries decode getPosition/getMovingState/getErrors into RAM variables with no mesh traffic; a missing or short reply times out keeping the value; an extra reply byte leaks to W2's Maestro", needs=NEEDS)
def get_local_replies(bench):
    """handleMaestroGet (WCB_Maestro.cpp:396-458) owns the port for 25 ms and reads exactly the reply length. The local
    slot wins over W1's M1:W20 proxy (localPort is set, so nothing is forwarded, :427). A byte beyond the reply length
    is left for KyberRemoteTask, which bridges it to W2's Maestro (WCB.ino:4828-4850)."""
    s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    names = ("m1pos0", "m1pos5", "m1moving", "m1err")
    bad = []

    def query(cmd, frame, var):
        m, wm = s1.mark(), w.dev.mark()
        w.send(cmd)
        try:
            s1.expect(frame, timeout=2, since=m)
        except AssertionError:
            bad.append(f"{cmd}: W1 S1 never sent {frame.hex(' ')}")
        time.sleep(0.4)
        return m, w.dev.since(wm), _get(w, var)

    try:
        s1.listen()
        tap.listen()
        s1.probe.rule_clear()
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
        w.run("?DEBUG,MAESTRO,ON")
        w.run("?DEBUG,ETM,ON")
        s1.rule(1, "AA011000", bytes.fromhex("7017"))
        s1.rule(2, "AA011005", bytes.fromhex("E803"))
        s1.rule(3, "AA0113", bytes.fromhex("01"))
        s1.rule(4, "AA0121", bytes.fromhex("0400"))
        for cmd, frame, var, rule, line, value in (
                (";M1,getPosition,0", "AA011000", "m1pos0", 1, "[MAESTRO] get 1 'getPosition' -> m1pos0=6000", 6000),
                (";M1,getPosition,5", "AA011005", "m1pos5", 2, "[MAESTRO] get 1 'getPosition' -> m1pos5=1000", 1000),
                (";M1,getMovingState", "AA0113", "m1moving", 3, "[MAESTRO] get 1 'getMovingState' -> m1moving=1", 1),
                (";M1,getErrors", "AA0121", "m1err", 4, "[MAESTRO] get 1 'getErrors' -> m1err=4", 4)):
            m, lines, got = query(cmd, bytes.fromhex(frame), var)
            if not s1.probe.rule_hits(rule, m):
                bad.append(f"{cmd}: reply rule {rule} never fired")
            if line not in lines:
                bad.append(f"{cmd}: no '{line}'")
            if got != f"[VAR] {var} = {value}":
                bad.append(f"{cmd}: {got}")
            if [x for x in lines if re.match(r"^\[ETM\] Sent seq \d+: ;M", x)] or _has(lines, " get '"):
                bad.append(f"{cmd}: a local query went to the mesh: {lines}")
        s1.probe.rule_del(3)
        _, lines, got = query(";M1,getMovingState", bytes.fromhex("AA0113"), "m1moving")
        if "[MAESTRO] get 1 'getMovingState': timeout (0/1 bytes) — value kept" not in lines or got != "[VAR] m1moving = 1":
            bad.append(f"no reply: {got}, {lines}")
        s1.probe.rule_del(1)
        s1.rule(5, "AA011000", bytes.fromhex("70"))
        _, lines, got = query(";M1,getPosition,0", bytes.fromhex("AA011000"), "m1pos0")
        if "[MAESTRO] get 1 'getPosition': timeout (1/2 bytes) — value kept" not in lines or got != "[VAR] m1pos0 = 6000":
            bad.append(f"short reply: {got}, {lines}")
        s1.rule(7, "AA0113", bytes.fromhex("0155"))
        watch = Watch(tap)
        _, lines, got = query(";M1,getMovingState", bytes.fromhex("AA0113"), "m1moving")
        time.sleep(1.0)
        if got != "[VAR] m1moving = 1":
            bad.append(f"extra byte: {got}")
        if watch.got(tap) != b"\x55":
            bad.append(f"the extra reply byte 55 should reach W2 S1 through the Kyber bridge, the tap got {watch.got(tap).hex(' ') or 'nothing'}")
    finally:
        s1.probe.rule_clear()          # a leftover rule answers later get frames, and those answers reach Maestro 2
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
        w.run("?DEBUG,MAESTRO,OFF")
        w.run("?DEBUG,ETM,OFF")
        _settle_maestro2(w)
    assert not bad, "; ".join(bad)


@test("maestro.get_moving_state_normalised", "(should) getMovingState stores 0/1, as its own comments and WcbCmd's decoder (NaviCore) do, not the raw reply byte", needs=NEEDS)
def get_moving_state_normalised(bench):
    """WCB stores reply[0] (WCB_Maestro.cpp:455) and forwards it as ;M!m<d>moving=<v>; the comments say 0/1
    (WCB_Maestro.cpp:380, WCB_Maestro.h) and WcbMaestro::decodeReply returns bytes[0] ? 1 : 0, which NaviCore uses."""
    s1 = link(bench, 1, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    try:
        s1.listen()
        s1.probe.rule_clear()
        w.run("?VAR,CLEAR,m1moving")
        s1.rule(1, "AA0113", bytes.fromhex("05"))
        m = s1.mark()
        w.send(";M1,getMovingState")
        s1.expect(bytes.fromhex("AA0113"), timeout=2, since=m)
        time.sleep(0.4)
        got = _get(w, "m1moving")
    finally:
        s1.probe.rule_clear()
        w.run("?VAR,CLEAR,m1moving")
    assert got == "[VAR] m1moving = 1", f"a moving-state reply of 05 should read as 1, got {got}"


@test("maestro.get_edge_cases", "Get-query rejections (no channel, channel 128, 16-char variable name, device 9/0, unknown query) never touch the wire; a persistent variable is not demoted by a reply", needs=NEEDS)
def get_edge_cases(bench):
    """Frame validation (WcbMaestro::build) runs before the query engine, so 'unknown query' is reachable only through
    the forwarded ;MG form (WCB_Maestro.cpp:400-402). All rejections print under ?DEBUG,ON, not ?DEBUG,MAESTRO
    (WCB_Maestro.cpp:279, 397, 401, 404). A RAM set refuses to demote a persistent variable (WCB_Variables.cpp:151-155)."""
    s1 = link(bench, 1, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1):
        try:
            s1.listen()
            s1.probe.rule_clear()
            w.run("?DEBUG,ON")
            w.run("?DEBUG,MAESTRO,ON")
            m, wm = s1.mark(), w.dev.mark()
            for cmd in (";M1,getPosition", ";M1,getPosition,128", ";M9,getErrors",
                        ";M0,getErrors", ";MG1,1,getFoo"):
                w.run(cmd)
            lines = w.dev.since(wm)
            for want in ("[MAESTRO] bad verb, ignored: 1,getPosition", "[MAESTRO] bad verb, ignored: 1,getPosition,128",
                         "[MAESTRO] get: bad device 9 (1-8 only)",
                         "[MAESTRO] get: bad device 0 (1-8 only)", "[MAESTRO] get: unknown query 'getFoo'"):
                if want not in lines:
                    bad.append(f"no '{want}'")
            time.sleep(1.0)
            if s1.received(m):
                bad.append(f"a rejected query reached W1 S1: {s1.received(m).hex(' ')}")
            # The channel is normalised once (tracker #29): ',00000000005' IS channel 5, so it is a legal query named
            # m1pos5 - the old 'var name too long' rejection came from building the name from the raw text.
            m = s1.mark()
            w.run(";M1,getPosition,00000000005")
            time.sleep(0.5)
            if s1.received(m) != bytes.fromhex("AA011005"):
                bad.append(f";M1,getPosition,00000000005 wrote {s1.received(m).hex(' ')}, expected aa 01 10 05")
            if not _has(w.run("?VAR,SET,m1err,77"), "[VAR] m1err = 77  [persistent]"):
                bad.append("?VAR,SET,m1err,77 did not confirm")
            s1.rule(3, "AA0121", bytes.fromhex("0400"))
            wm = w.dev.mark()
            w.send(";M1,getErrors")
            time.sleep(0.8)
            got = _get(w, "m1err")
            if got != "[VAR] m1err = 77":
                bad.append(f"a reply demoted the persistent m1err: {got}")
            if "[MAESTRO] get 1 'getErrors' -> m1err=4" in w.dev.since(wm):
                bench.note("the debug line reports 'm1err=4' although the refused RAM set kept 77 (WCB_Maestro.cpp:457-458)")
        finally:
            s1.probe.rule_clear()
            w.run("?VAR,CLEAR,m1err")
            w.run("?VAR,CLEAR,m1pos5")
            w.run("?DEBUG,OFF")
            w.run("?DEBUG,MAESTRO,OFF")
    assert not bad, "; ".join(bad)


@test("maestro.get_var_name_from_parsed_channel", "(should) The RAM variable of getPosition uses the parsed channel like the frame does: ',05' fills m1pos5 and a trailing field still fills m1pos0", needs=NEEDS)
def get_var_name_from_parsed_channel(bench):
    """The name is 'm'+dev+'pos'+<raw channel text> (WCB_Maestro.cpp:389) while the frame uses the parsed integer, so
    ',05' stores m1pos05 (an IF on m1pos5 never sees it), and ',0,99' builds a valid frame but the name 'm1pos0,99',
    whose failed store is ignored (WCB_Maestro.cpp:457)."""
    s1 = link(bench, 1, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    names = ("m1pos5", "m1pos05", "m1pos0")
    bad = []
    try:
        s1.listen()
        s1.probe.rule_clear()
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
        s1.rule(1, "AA011005", bytes.fromhex("7017"))
        s1.rule(2, "AA011000", bytes.fromhex("7017"))
        for cmd, frame, var in ((";M1,getPosition,05", "AA011005", "m1pos5"), (";M1,getPosition,0,99", "AA011000", "m1pos0")):
            m = s1.mark()
            w.send(cmd)
            s1.expect(bytes.fromhex(frame), timeout=2, since=m)
            time.sleep(0.4)
            got = _get(w, var)
            if got != f"[VAR] {var} = 6000":
                bad.append(f"{cmd} should fill {var}, got {got}")
    finally:
        s1.probe.rule_clear()
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
    assert not bad, "; ".join(bad)


# ============================================================ broadcast and fallbacks
@test("maestro.m0_broadcast", ";M0,stopScript writes one frame per local Maestro, re-addressed to its own id, on W1 and W2 from one ETM broadcast; ;M0<n> and ;M0,<n> are the same subroutine broadcast", needs=NEEDS)
def m0_broadcast(bench):
    """Broadcast branches: WCB_Maestro.cpp:160-184 (subroutine) and :332-342 (verb). The extra legacy S1 frame is written
    only when no slot has the board's own number (:170, :336), which both bench boards have. The subroutine arms need
    bench.json "maestro_safe_sub" — they run a real script on Maestro 2 and every NaviCore Maestro."""
    s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    bad = []
    try:
        s1.listen()
        tap.listen()
        s1.probe.rule_clear()
        w.run("?DEBUG,ETM,ON")
        arms = [(";M0,stopScript", ";M0,stopScript", "AA0124", "AA0224")]
        sub = bench.cfg.get("maestro_safe_sub")
        if isinstance(sub, int) and 0 <= sub <= 127:
            arms += [(f";M0{sub}", f";M0{sub}", f"AA0127{sub:02X}", f"AA0227{sub:02X}"),
                     (";M0,stopScript", ";M0,stopScript", "AA0124", "AA0224"),
                     (f";M0,{sub}", f";M0{sub}", f"AA0127{sub:02X}", f"AA0227{sub:02X}"),
                     (";M0,stopScript", ";M0,stopScript", "AA0124", "AA0224")]
        else:
            bench.note('subroutine arms skipped: set bench.json "maestro_safe_sub" to a subroutine safe on Maestro 2 and NaviCore')
        for cmd, text, f1, f2 in arms:
            m1, mt, wm = s1.mark(), tap.mark(), w.dev.mark()
            w.send(cmd)
            for problem in (_exact(s1, m1, bytes.fromhex(f1), settle=0.2), _exact(tap, mt, bytes.fromhex(f2), settle=1.8)):
                if problem:
                    bad.append(f"{cmd}: {problem}")
            lines = w.dev.since(wm)
            seqs = _sent_seq(lines, text)
            if len(seqs) != 1:
                bad.append(f"{cmd}: expected one '[ETM] Sent seq N: {text}', got {seqs}")
            elif not _acked_by(lines, 2, seqs[0]):
                bad.append(f"{cmd}: WCB2 never ACKed seq {seqs[0]}")
    finally:
        w.run("?DEBUG,ETM,OFF")
    assert not bad, "; ".join(bad)


@test("maestro.fallback_broadcast_unconfigured", "An unconfigured id above WCBQ (5-8) falls back to one ACKed ETM broadcast for a subroutine, a verb, sub+param and a get; receivers drop it; ;M<id>256 is ignored", needs=NEEDS)
def fallback_broadcast_unconfigured(bench):
    """sendMaestroCommand WCB_Maestro.cpp:232-238 (printed with no debug flag), verbs :365-372, gets :427-434. A board
    with no slot for the id drops the broadcast silently because it arrived over ESP-NOW (:233, :367, :428). ;M<id>256
    fails processMaestroCommand's 0-255 check. Ids NaviCore hosts are excluded: the subroutine arms would run there."""
    s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    l1, l2 = _require_remote_pair(bench)
    w = usb_wcb(bench)
    q = token(snapshot(bench, 1), "?WCBQ,")
    wcbq = int(q[len("?WCBQ,"):]) if q else 0
    used = {int(t[len("?MAESTRO,M")]) for t in l1 + l2} | _navicore_hosted(bench, l1)
    free = [i for i in range(5, 9) if i > wcbq and i not in used]
    if not free:
        raise Skip(f"no Maestro id 5-8 is above WCBQ {wcbq} and unhosted on W1, W2 and NaviCore")
    a, b = free[0], free[-1]
    bad = []
    try:
        s1.listen()
        tap.listen()
        s1.probe.rule_clear()
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}ON")
        w.run(f"?VAR,CLEAR,m{a}moving")
        watch = Watch(s1, tap)
        for cmd, text, line in (
                (f";M{a}1", f";M{a}1", f"→ Maestro {a}: Fallback broadcast, Script 1"),
                (f";M{a},goHome", f";M{a},goHome", f"→ Maestro {a} verb '{a},goHome': Fallback broadcast"),
                (f";M{a},1,500", f";M{a},1,500", f"→ Maestro {a} verb '{a},1,500': Fallback broadcast"),
                (f";M{a},getMovingState", f";MG{a},1,getMovingState", f"→ Maestro {a} get 'getMovingState' -> broadcast, id {a} (reply to 1)"),
                (f";M{b},99", f";M{b}99", f"→ Maestro {b}: Fallback broadcast, Script 99")):
            wm = w.dev.mark()
            w.send(cmd)
            time.sleep(1.2)
            lines = w.dev.since(wm)
            if line not in lines:
                bad.append(f"{cmd}: no '{line}'")
            seqs = _sent_seq(lines, text)
            if len(seqs) != 1:
                bad.append(f"{cmd}: expected one '[ETM] Sent seq N: {text}', got {seqs}")
            elif not _acked_by(lines, 2, seqs[0]):
                bad.append(f"{cmd}: WCB2 never ACKed seq {seqs[0]}, so it was not a broadcast")
        got = _get(w, f"m{a}moving")
        if got != _not_set(f"m{a}moving"):
            bad.append(f"nobody hosts Maestro {a}, yet {got}")
        wm = w.dev.mark()
        w.send(f";M{a}256")
        time.sleep(1.0)
        # Any SEND or routing line counts; W1's own "Processing input from Serial0: ;M<a>256" echo (debug is on) does not.
        noise = [x for x in w.dev.since(wm) if not x.startswith("Processing input from")
                 and (f"Maestro {a}" in x or f": ;M{a}256" in x)]
        if noise:
            bad.append(f";M{a}256 was not ignored: {noise}")
        try:
            watch.silent(s1, tap, window=1.5)
        except AssertionError as e:
            bad.append(f"a fallback reached a Maestro wire: {e}")
    finally:
        s1.probe.rule_clear()
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}OFF")
    assert not bad, "; ".join(bad)


@test("maestro.get_fallback_unicast_within_wcbq", "(should) A get for an unconfigured id within WCBQ is unicast to WCB<id> like the subroutine and verb fallbacks, not broadcast to every board", needs=["wcb1"], links=[])
def get_fallback_unicast_within_wcbq(bench):
    """Subroutines and verbs for an unconfigured id 1..WCBQ unicast to WCB<id> (WCB_Maestro.cpp:223-226, :368); a get
    with no host proxy always broadcasts ;MG (:432), so every board and NaviCore receive it and any host of that id
    answers. Observed with WCBQ raised to 5 and id 5: WCB2 ACKs a broadcast, never a unicast to WCB5."""
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        if token(before[1], "?WCBQ,") != "?WCBQ,2":
            raise Skip("W1 is not ?WCBQ,2")
        if any(t.upper().startswith("?MAESTRO,M5:") for t in before[1]):
            raise Skip("W1 has a Maestro 5 slot")
        try:
            if not _has(w.run("?WCBQ,5"), "Saved WCB quantity: 5."):
                raise AssertionError("?WCBQ,5 did not confirm")
            w.run("?DEBUG,ETM,ON")
            w.run("?VAR,CLEAR,m5err")
            wm = w.dev.mark()
            w.send(";M5,goHome")
            time.sleep(2.0)
            control = w.dev.since(wm)
            wm = w.dev.mark()
            w.send(";M5,getErrors")
            time.sleep(2.0)
            lines = w.dev.since(wm)
        finally:
            w.run("?DEBUG,ETM,OFF")
            w.run("?VAR,CLEAR,m5err")
            w.run("?WCBQ,2")
    seqs = _sent_seq(control, ";M5,goHome")
    if seqs and _acked_by(control, 2, seqs[0]):
        raise Skip("WCB2 ACKed the ;M5,goHome unicast as well, so an ACK cannot tell unicast from broadcast here")
    got = _sent_seq(lines, ";MG5,1,getErrors")
    assert got, f"W1 never sent ;MG5,1,getErrors: {lines}"
    assert not _acked_by(lines, 2, got[0]), \
        f"the get went out as a broadcast (WCB2 ACKed seq {got[0]}), not as a unicast to WCB5"


@test("maestro.fallback_unicast_wcbq_probe", "An unconfigured id within WCBQ reaches WCB<id> as exactly one unicast for ;M<id><n>, a verb, ;M<id>,<n> and sub+param (the probe joined as client 5)", needs=["wcb1", "probe1"], links=[])
def fallback_unicast_wcbq_probe(bench):
    """WCB_Maestro.cpp:223-231 and :365-372. The probe only receives here, so its id needs no ring burn. WCBQ is raised
    to 5 so id 5 is a floor peer W1 can unicast to; floor and temporary peers are never persisted."""
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1) as before:
        if token(before[1], "?WCBQ,") != "?WCBQ,2":
            raise Skip("W1 is not ?WCBQ,2")
        if any(t.upper().startswith("?MAESTRO,M5:") for t in before[1]):
            raise Skip("W1 has a Maestro 5 slot")
        try:
            if not _has(w.run("?WCBQ,5"), "Saved WCB quantity: 5."):
                raise AssertionError("?WCBQ,5 did not confirm")
            wm = w.dev.mark()
            with probe_in_mesh(bench, "probe1", 5) as probe:
                w.dev.expect(r"\[ETM\] WCB5 came ONLINE", timeout=30, since=wm)
                w.run("?DEBUG,ON")
                w.run("?DEBUG,MAESTRO,ON")
                for cmd, text, line in (
                        (";M51", ";M51", "→ Maestro 5: Fallback unicast to WCB5, Script 1"),
                        (";M5,goHome", ";M5,goHome", "→ Maestro 5 verb '5,goHome': Fallback unicast"),
                        (";M5,7", ";M57", "→ Maestro 5: Fallback unicast to WCB5, Script 7"),
                        (";M5,sub,3,1000", ";M5,sub,3,1000", "→ Maestro 5 verb '5,sub,3,1000': Fallback unicast")):
                    pat = re.compile(rf"^{re.escape(text)}(?:\|[0-9A-Fa-f]+)?$")
                    pm, wm = probe.dev.mark(), w.dev.mark()
                    w.send(cmd)
                    hit = _wait_rx(probe, pm, 1, pat.match, timeout=4)
                    time.sleep(1.0)
                    copies = sum(1 for s, x in probe.mesh_received(pm) if s == 1 and pat.match(x))
                    if hit is None:
                        bad.append(f"{cmd}: the probe never received {text}: {probe.mesh_received(pm)}")
                    elif copies != 1:
                        bad.append(f"{cmd}: the probe received {copies} copies")
                    if line not in w.dev.since(wm):
                        bad.append(f"{cmd}: no '{line}'")
        finally:
            w.run("?DEBUG,OFF")
            w.run("?DEBUG,MAESTRO,OFF")
            w.run("?WCBQ,2")
    assert not bad, "; ".join(bad)


@test("maestro.stale_proxy_dropped", "A W1 proxy to W2 for an id W2 does not host is forwarded and ACKed, but W2 drops it: nothing on W2 S1, and the get never answers", needs=["wcb1"])
def stale_proxy_dropped(bench):
    """Proxies are never evicted (WCB_Maestro.cpp:719-720). W2 gets the unicast over ESP-NOW with no slot for the id and
    not its own number, so every fallback is skipped (:224, :367, :428) — ETM delivery is not execution."""
    tap = link(bench, 2, "S1")
    l1, l2 = _require_remote_pair(bench)
    w = usb_wcb(bench)
    ids = [int(m.group(1)) for m in (re.match(r"^\?MAESTRO,M(\d):W2S", t, re.I) for t in l1) if m]
    x = next((i for i in ids if i != 2 and not _slot(l2, i, 2) and not _slot(l1, i, 1)), None)
    if x is None:
        raise Skip("W1 has no proxy to W2 for an id that neither W2 nor W1 hosts")
    bad = []
    try:
        tap.listen()
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}ON")
        w.run(f"?VAR,CLEAR,m{x}err")
        watch = Watch(tap)
        for cmd, text, line in (
                (f";M{x}1", f";M{x}1", f"→ Maestro {x}: Unicast to WCB2, Script 1"),
                (f";M{x},goHome", f";M{x},goHome", f"→ Maestro {x} verb '{x},goHome': Unicast WCB2"),
                (f";M{x},getErrors", f";MG{x},1,getErrors", f"→ Maestro {x} get 'getErrors' -> WCB2 (reply to 1)")):
            wm = w.dev.mark()
            w.send(cmd)
            time.sleep(1.5)
            lines = w.dev.since(wm)
            if line not in lines:
                bad.append(f"{cmd}: no '{line}'")
            if _has(lines, "Fallback"):
                bad.append(f"{cmd}: a proxy-handled id fell back: {lines}")
            seqs = _sent_seq(lines, text)
            if len(seqs) != 1 or not _acked_by(lines, 2, seqs[0]):
                bad.append(f"{cmd}: expected one ACKed '[ETM] Sent seq N: {text}', got {seqs}")
        got = _get(w, f"m{x}err")
        if got != _not_set(f"m{x}err"):
            bad.append(f"W2 does not host Maestro {x}, yet {got}")
        try:
            watch.silent(tap, window=1.0)
        except AssertionError as e:
            bad.append(f"W2 acted on a stale proxy command: {e}")
    finally:
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}OFF")
    assert not bad, "; ".join(bad)


@test("maestro.mesh_get_rewrite_probe", "A client's plain ;M1,get... is answered to the sender and stored on the host; explicit ;MG with replyTo 21 or 0 is dropped; a chained get stays local; ;M! accepts only m1-m8 names", needs=["wcb1", "probe1", "probe2"])
def mesh_get_rewrite_probe(bench):
    """maestroRewriteInboundGet (WCB_Maestro.cpp:515-528) makes an inbound standalone get ;MG<dev>,<sender>,…; a replyTo
    outside 1..20 is dropped before any read (:487); a WCB asker gets ;M!<var>=<v> (:474); a '^' chain is exempt (:517);
    handleMaestroResult takes only names m1..m8 (:502). Never ;MG…,20 — that pushes :MQR into NaviCore."""
    s1 = link(bench, 1, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    cid = 10
    vx, vy = f"hil{nonce().lower()}", f"m9hil{nonce().lower()}"
    names = ("m1pos0", "m1moving", vx, vy)
    bad = []

    def result(probe, since, name):
        return _wait_rx(probe, since, 1, lambda x: x.startswith(f";M!{name}="), timeout=4)

    try:
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
        w.run("?DEBUG,MAESTRO,ON")
        wm = w.dev.mark()
        with probe_in_mesh(bench, "probe2", cid) as probe:
            w.dev.expect(rf"\[WDP\] temporarily joined WCB{cid} ", timeout=15, since=wm)
            _burn_ring(w, probe)
            s1.listen()
            s1.probe.rule_clear()
            s1.rule(1, "AA011000", bytes.fromhex("7017"))
            s1.rule(2, "AA0113", bytes.fromhex("00"))

            pm, m, wm = probe.dev.mark(), s1.mark(), w.dev.mark()
            probe.mesh_send(1, ";M1,getPosition,0")
            if _exact(s1, m, bytes.fromhex("AA011000"), settle=0.2):
                bad.append("plain get: W1 S1 did not get exactly AA 01 10 00")
            rx = result(probe, pm, "m1pos0")
            if not rx or not re.match(r"^;M!m1pos0=6000(?:\|[0-9A-Fa-f]+)?$", rx):
                bad.append(f"plain get: the probe got {rx}")
            if f"[MAESTRO] get reply -> WCB{cid}: ;M!m1pos0=6000" not in w.dev.since(wm):
                bad.append("plain get: no 'get reply' line")
            if _get(w, "m1pos0") != "[VAR] m1pos0 = 6000":
                bad.append("plain get: the host did not store m1pos0 too")

            pm, m = probe.dev.mark(), s1.mark()
            probe.mesh_send(1, f";MG1,{cid},getMovingState")
            rx = result(probe, pm, "m1moving")
            if _exact(s1, m, bytes.fromhex("AA0113"), settle=0.2) or not rx or not rx.startswith(";M!m1moving=0"):
                bad.append(f";MG with replyTo {cid}: probe got {rx}, S1 got {s1.received(m).hex(' ')}")

            for rt in (21, 0):
                pm, m = probe.dev.mark(), s1.mark()
                probe.mesh_send(1, f";MG1,{rt},getMovingState")
                time.sleep(1.5)
                if s1.received(m) or [x for s, x in probe.mesh_received(pm) if x.startswith(";M!")]:
                    bad.append(f";MG replyTo {rt} was not dropped before the read")

            t = marker("CH")
            pm, m, wm = probe.dev.mark(), s1.mark(), w.dev.mark()
            probe.mesh_send(1, f";M1,getMovingState^;S0,{t}")
            try:
                w.dev.expect(rf"^{t}$", timeout=3, since=wm)
            except AssertionError:
                bad.append("chain: the ;S0 half never ran")
            time.sleep(1.5)
            if bytes.fromhex("AA0113") not in s1.received(m):
                bad.append("chain: the get half never queried W1 S1")
            if [x for s, x in probe.mesh_received(pm) if x.startswith(";M!")]:
                bad.append("chain: a chained get was answered to the sender (the rewrite should skip chains)")

            for text in (";M!m1pos0=1234", f";M!{vx}=9", f";M!{vy}=5"):
                probe.mesh_send(1, text)
            time.sleep(1.0)
            for name, want in (("m1pos0", "[VAR] m1pos0 = 1234"), (vx, _not_set(vx)), (vy, _not_set(vy))):
                got = _get(w, name)
                if got != want:
                    bad.append(f";M! {name}: {got}")
    finally:
        s1.probe.rule_clear()
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
        w.run("?DEBUG,MAESTRO,OFF")
    assert not bad, "; ".join(bad)


# ============================================================ the slot table
@test("maestro.clear_all_legacy_routing", "?MAESTRO,CLEAR,ALL output, then legacy routing (own id and target 9 on S1, the dev-0 extra S1 frame, unicast fallback to the real Maestro 2, a unicast get); the table is rebuilt in backup order", needs=NEEDS)
def clear_all_legacy_routing(bench):
    """clearAllMaestroConfigs WCB_Maestro.cpp:869-922; legacy branches :204-219 and :345-363; the dev-0 extra S1 frame
    :336-337; fallbacks :367-372; a get on the board's own number reads S1 (:423) and any other unhosted get unicasts to
    WCB<id> inside WCBQ like the verb fallback, broadcasting only above it (handleMaestroGet; tracker #52). The clear resets S1 to 9600 (:912-918) although legacy routing still writes a Maestro there — noted only."""
    s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1, 2) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        if token(before[1], "?WCBQ,") != "?WCBQ,2":
            raise Skip("W1 is not ?WCBQ,2")
        if [t for t in lines if re.match(r"^\?MAESTRO,M\d:W1S[2-5]:", t, re.I)]:
            raise Skip("W1 has a local Maestro off S1")
        bench.note("restore if aborted: ?WDP,OFF, ?MAESTRO,CLEAR,ALL, then " + " , ".join(lines) + " , ?WDP,ON")
        with _wdp_off(w, before[1]):
            try:
                tap.listen()
                for kind in ("", "MAESTRO,", "ETM,"):
                    w.run(f"?DEBUG,{kind}ON")
                needles = ["All Maestro configurations cleared", "Reverted to legacy routing (Maestro ID = WCB Number on S1)",
                           "Re-enabling broadcasts on freed ports:"]
                needles += ["✓ S1 output enabled"] if "?BCAST,OUT,S1,OFF" in before[1] else []
                needles += ["✓ S1 input enabled"] if "?BCAST,IN,S1,OFF" in before[1] else []
                needles += ["Resetting baud rates on freed ports:", "Baud rate for Serial1 updated to 9600", "✓ S1 baud rate reset to 9600"]
                bad += _in_order(w.run("?MAESTRO,CLEAR,ALL", timeout=8), needles, "CLEAR,ALL")
                if not _has(w.run("?MAESTRO,LIST"), "  No Maestros configured"):
                    bad.append("LIST after CLEAR,ALL is not empty")
                s1.listen(9600)
                s1.probe.rule_clear()
                s1.rule(1, "AA0121", bytes.fromhex("0200"))
                for n in ("m1err", "m2err"):
                    w.run(f"?VAR,CLEAR,{n}")
                for cmd, f1, f2, line, text in (
                        (";M12", "AA012702", "", "→ Maestro 1: Legacy S1, Script 2", None),
                        (";M1,goHome", "AA0122", "", "→ Maestro 1 verb '1,goHome': Legacy S1", None),
                        (";M95", "AA012705", "", "→ Maestro (local, target 9): legacy S1 fallback, dev 1, Script 5", None),
                        (";M9,stopScript", "AA0124", "", "→ Maestro (local, target 9) verb '9,stopScript'", None),
                        (";M1,getErrors", "AA0121", "", "[MAESTRO] get 1 'getErrors' -> m1err=2", None),
                        (";M0,stopScript", "AA0124", "AA0224", "→ Maestro Broadcast verb '0,stopScript'", ";M0,stopScript"),
                        (";M2,stopScript", "", "AA0224", "→ Maestro 2 verb '2,stopScript': Fallback unicast", ";M2,stopScript"),
                        (";M2,getErrors", "", "AA0221", "→ Maestro 2 get 'getErrors' -> WCB2 (reply to 1)", ";MG2,1,getErrors"),
                        (";M3,stopScript", "", "", "→ Maestro 3 verb '3,stopScript': Fallback broadcast", ";M3,stopScript")):
                    m1, mt, wm = s1.mark(), tap.mark(), w.dev.mark()
                    w.send(cmd)
                    for l, since, frame in ((s1, m1, f1), (tap, mt, f2)):
                        problem = _exact(l, since, bytes.fromhex(frame), settle=0.8)
                        if problem:
                            bad.append(f"{cmd}: {problem}")
                    got = w.dev.since(wm)
                    if line not in got:
                        bad.append(f"{cmd}: no '{line}'")
                    seqs = _sent_seq(got, text) if text else []
                    if text and (len(seqs) != 1 or not _acked_by(got, 2, seqs[0])):
                        bad.append(f"{cmd}: expected one '[ETM] Sent seq N: {text}' ACKed by WCB2, got {seqs}")
                    if not text and [x for x in got if x.startswith("[ETM] Sent seq") and ": ;M" in x]:
                        bad.append(f"{cmd}: a legacy local command went to the mesh")
                if _get(w, "m1err") != "[VAR] m1err = 2":
                    bad.append(f"legacy S1 get: {_get(w, 'm1err')}")
                got = _get(w, "m2err") or ""
                if not re.match(r"^\[VAR\] m2err = \d+$", got):
                    bad.append(f"the unicast get was not answered by W2: {got}")
                bench.note("after CLEAR,ALL the legacy S1 routing writes at 9600, the rate the clear forced (WCB_Maestro.cpp:912-918)")
            finally:
                s1.probe.rule_clear()
                for n in ("m1err", "m2err"):
                    w.run(f"?VAR,CLEAR,{n}")
                for kind in ("", "MAESTRO,", "ETM,"):
                    w.run(f"?DEBUG,{kind}OFF")
                bad += _rebuild(w, lines)
    assert not bad, "; ".join(bad)


@test("maestro.legacy_clear_suffix_keeps_slots", "(should) A '?MAESTRO_CLEAR,M5' typo does not wipe every slot; the legacy handler prefix-matches and runs clearAllMaestroConfigs (WCB.ino:5796-5797)", needs=["wcb1"], links=[])
def legacy_clear_suffix_keeps_slots(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        if not lines:
            raise Skip("W1 has no Maestro slots")
        _require_replayable(lines)
        if any(t.upper().startswith("?MAESTRO,M5:") for t in lines):
            raise Skip("W1 has a Maestro 5 slot")
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?MAESTRO_CLEAR,M5", timeout=8)
                left = [x for x in w.run("?MAESTRO,LIST") if x.startswith("  Maestro ")]
            finally:
                problems = _rebuild(w, lines)
    assert not problems, "; ".join(problems)
    assert len(left) == len(lines) and not _has(out, "All Maestro configurations cleared"), \
        f"'?MAESTRO_CLEAR,M5' wiped the table: {len(left)} of {len(lines)} slots left, output {out}"


@test("maestro.slot_map_capacity_clear_variants", "Re-issued backup lines map the slots; placeholders fill the free slots in order up to the 9-slot cap; a remote key ignores the port; every ?MAESTRO,CLEAR form and refusal", needs=["wcb1"], links=[])
def slot_map_capacity_clear_variants(bench):
    """configureMaestro keys a slot on (id, port, remote) with port 0 for a remote (WCB_Maestro.cpp:620-636, :687-697);
    the full-table check runs before the software-serial block (:629-656); clearMaestroByID ignores a remote target's
    port (:852-856). Hosts W11+ are placeholders; remote proxies are never advertised and WDP is off, so nothing outside
    W1 changes and no real advert can take a slot mid-test."""
    w = usb_wcb(bench)
    bad = []
    invalid_id = "Invalid Maestro ID. Must be M1-M8 (9=all local, 0=all are reserved)"
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        if len(lines) > 5:
            raise Skip(f"W1 has {len(lines)} Maestro slots; this needs 4 free")
        taken = [t for t in lines if re.match(r"^\?MAESTRO,M(8:|5:|1:W1S[23]:)", t, re.I)]
        if taken:
            raise Skip(f"W1 already has {taken}")
        ref = [x for x in w.run("?MAESTRO,LIST") if x.startswith("  ")]
        with _wdp_off(w, before[1]):
            try:
                used = []
                for t in lines:
                    out = w.run(t)
                    m = next((re.search(r"Maestro \d: (?:Local S\d at \d+ baud \(|Remote on WCB\d+ \(unicast, )slot (\d)\)", x)
                              for x in out if "slot" in x), None)
                    if not m or _has(out, "Baud rate for Serial") or _has(out, "broadcast"):
                        bad.append(f"re-issuing {t} printed {out}")
                    else:
                        used.append(int(m.group(1)))
                free = sorted(set(range(1, 10)) - set(used))
                for h, s in zip(range(11, 11 + len(free)), free):
                    want = f"✓ Maestro 8: Remote on WCB{h} (unicast, slot {s})"
                    if not _has(w.run(f"?MAESTRO,M8:W{h}S1:9600"), want):
                        bad.append(f"placeholder W{h}: no '{want}'")
                for cmd in (f"?MAESTRO,M8:W{11 + len(free)}S1:9600", "?MAESTRO,M1:W1S3:115200"):
                    out = w.run(cmd)
                    if not _has(out, "No available slots. Maximum 9 Maestros.") or _has(out, "SOFTWARE SERIAL"):
                        bad.append(f"full table: {cmd} printed {out}")
                if not _has(w.run("?MAESTRO,M8:W11S4:57600"), f"✓ Maestro 8: Remote on WCB11 (unicast, slot {free[0]})"):
                    bad.append("M8:W11 given port S4 did not reuse its slot")
                toks = snapshot(bench, 1)
                if "?MAESTRO,M8:W11S1:57600" not in toks or "?MAESTRO,M8:W11S4:57600" in toks:
                    bad.append(f"the backup should hold M8:W11 as S1 at 57600: {[t for t in toks if 'M8:' in t]}")
                full = [x for x in w.run("?MAESTRO,LIST") if x.startswith("  Maestro ")]
                if len(full) != 9 or "  Maestro 8 → WCB11" not in full:
                    bad.append(f"full LIST: {full}")
                left = len(free) - 3
                for cmd, want in (("?MAESTRO,CLEAR,M8:W11S5", f"Cleared Maestro M8:W11S5 (freed slot {free[0]})"),
                                  ("?MAESTRO,CLEAR,m8:w12s1", f"Cleared Maestro M8:W12S1 (freed slot {free[1]})"),
                                  ("?MAESTRO,CLEAR,8:W13S1:9600", f"Cleared Maestro M8:W13S1 (freed slot {free[2]})"),
                                  ("?MAESTRO,CLEAR,M8:W13S1", "Maestro M8:W13S1 is not configured"),
                                  ("?MAESTRO,CLEAR,M8", f"Cleared Maestro ID 8 ({left} slot{'' if left == 1 else 's'})"),
                                  ("?MAESTRO,CLEAR,M8", "Maestro ID 8 is not configured"),
                                  ("?MAESTRO,CLEAR,M9", invalid_id),
                                  ("?MAESTRO,CLEAR,M0", invalid_id),
                                  ("?MAESTRO,CLEAR,M1:X", "Invalid CLEAR target 'M1:X'. Use M<id>:W<wcb>S<port> or M<id>"),
                                  ("?MAESTRO,CLEAR,M1:W1S2", "Maestro M1:W1S2 is not configured"),
                                  ("?MAESTRO,CLEAR,M5", "Maestro ID 5 is not configured"),
                                  ("?MAESTRO,CLEAR,", invalid_id),
                                  ("?MAESTRO,CLEAR", "Invalid format. Use: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>")):
                    out = w.run(cmd)
                    if not _has(out, want):
                        bad.append(f"{cmd}: no '{want}' in {out}")
                after = [x for x in w.run("?MAESTRO,LIST") if x.startswith("  ")]
                if after != ref:
                    bad.append(f"LIST after the clears {after} is not the starting {ref}")
            finally:
                w.run("?MAESTRO,CLEAR,M8")
    assert not bad, "; ".join(bad)


@test("maestro.config_negatives", "?MAESTRO add-path refusals change nothing; a chained config reports each bad entry; the software-serial baud block; a remote target skips that block", needs=["wcb1"], links=[])
def config_negatives(bench):
    """configureMaestro WCB_Maestro.cpp:536-618 and the local-only software-serial block :643-656."""
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        if len(lines) > 8 or [t for t in lines if re.match(r"^\?MAESTRO,M(8:|1:W1S3:)", t, re.I)]:
            raise Skip("W1 needs a free slot, no Maestro 8 and no M1 on S3")
        for cmd, needles in (
                ("?MAESTRO,M9:W1S1:57600", ["Invalid Maestro ID. Must be 1-8 (9=all local, 0=all Maestros are reserved)"]),
                ("?MAESTRO,M0:W1S1:57600", ["Invalid Maestro ID. Must be 1-8 (9=all local, 0=all Maestros are reserved)"]),
                ("?MAESTRO,M1:W21S1:57600", ["Invalid WCB number. Must be W1-W20"]),
                ("?MAESTRO,M1:W1S6:57600", ["Invalid serial port. Must be S1-S5"]),
                ("?MAESTRO,M1:W1S1:12345", ["Invalid baud rate"]),
                ("?MAESTRO,M1W1S1", ["Invalid Maestro config: M1W1S1", "Format should be: M<maestroID>:W<wcb>S<port>:<baud>"]),
                ("?MAESTRO,M1:1S1:57600", ["Destination must be in format W<wcb>S<port> (e.g., W2S1)"]),
                ("?MAESTRO,X1:W1S1:57600", ["Invalid format. Use: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>",
                                            "Example: ?MAESTRO,M2:W2S1:57600"]),
                ("?MAESTRO,M9:W1S1:57600,X2:W1S1:57600", ["Invalid Maestro ID. Must be 1-8",
                                                          "Invalid Maestro config: X2:W1S1:57600 (must start with M)"]),
                ("?MAESTRO,M1:W1S3:115200", ["=============== WARNING ===============", "S3 is SOFTWARE SERIAL",
                                             "Baud rate (115200) is TOO HIGH for software serial", "CONFIGURATION BLOCKED!",
                                             "Options:", "1. Use hardware serial: ?MAESTRO,M1:W1S1:115200",
                                             "2. Lower baud rate: ?MAESTRO,M1:W1S3:57600", "========================================"])):
            bad += _in_order(w.run(cmd), needles, cmd)
        if snapshot(bench, 1) != before[1]:
            bad.append("a refused ?MAESTRO changed the saved config")
        out = w.run("?MAESTRO,M8:W11S3:115200")
        m = next((re.search(r"Maestro 8: Remote on WCB11 \(unicast, slot (\d)\)", x) for x in out if "WCB11" in x), None)
        if not m or _has(out, "SOFTWARE SERIAL"):
            bad.append(f"a remote S3 target at 115200 should be accepted without the block: {out}")
        slot = m.group(1) if m else "?"
        if not _has(w.run("?MAESTRO,CLEAR,M8:W11S3"), f"Cleared Maestro M8:W11S3 (freed slot {slot})"):
            bad.append("the M8:W11S3 placeholder did not clear")
    assert not bad, "; ".join(bad)


@test("maestro.local_baud_zero_rejected", "(should) ?MAESTRO refuses baud 0 for a local slot instead of printing 'Invalid baud rate' and then saving 'Local S1 at 0 baud'", needs=["wcb1"], links=[])
def local_baud_zero_rejected(bench):
    """configureMaestro accepts 0 (WCB_Maestro.cpp:610); updateBaudRate refuses it (WCB_Storage.cpp:155-160) yet the slot
    is saved and printed (:677-685), so the backup claims 0 baud for a port still running its old rate. WDP is off, so
    the changed advert never leaves W1 (receivers skip baud 0 anyway, WCB_WDP.cpp:709)."""
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        orig = next((t for t in _m_lines(before[1]) if re.match(r"^\?MAESTRO,M1:W1S1:\d+$", t, re.I)), None)
        if not orig:
            raise Skip("W1 has no local Maestro 1 on S1")
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?MAESTRO,M1:W1S1:0")
                toks = snapshot(bench, 1)
            finally:
                w.run(orig)
    m1 = [t for t in toks if t.upper().startswith("?MAESTRO,M1:")]
    assert not _has(out, "Local S1 at 0 baud") and "?MAESTRO,M1:W1S1:0" not in toks, \
        f"baud 0 was accepted: {out}; the backup holds {m1}"


# ============================================================ Kyber
def _wait_bytes(watch, links, n, timeout):
    deadline = time.monotonic() + timeout
    while any(len(watch.got(l)) < n for l in links) and time.monotonic() < deadline:
        time.sleep(0.1)


@test("kyber.remote_roundtrip", "Maestro_Remote both ways: a getErrors frame into W1 S1 reaches W2's real Maestro and its reply comes back out of W1 S1; 64 bytes arrive in order (up to 3 tries on the best-effort channel) with no failed raw send", needs=NEEDS)
def remote_roundtrip(bench):
    """W1's KyberRemoteTask drains its Maestro port into non-ETM target-98 broadcasts (forwardMaestroDataToRemoteKyber,
    sendESPNowRaw); W2 writes them to its Maestro port, bridges the reply back, and W1 writes it to its own Maestro port
    (the target-98 branch of espNowReceiveCallback). The debug dump prints from the receive callback, so payloads stay
    small while it is on.

    The channel is best-effort by design (docs/WCB_KYBER_PASSTHROUGH_PARAMS.md §2): no ACK, no retry, and about 1 % of
    frames are lost on this bench, so the ~11-frame 64-byte burst loses a piece ~10 % of the time (tracker #74). It gets
    up to 3 tries. What a firmware bug would do - duplicate, reorder or corrupt a byte - fails at once: a lossy try must
    still be an in-order subsequence of the payload, whose bytes are all distinct."""
    s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    bad = []
    try:
        s1.listen()
        tap.listen()
        s1.probe.rule_clear()
        if not _has(w.run("?STATS,RESET"), "ESP-NOW statistics reset."):
            bad.append("?STATS,RESET did not confirm")
        w.run("?DEBUG,MAESTRO,ON")
        watch, wm = Watch(s1, tap), w.dev.mark()
        s1.send(GET_ERRORS_2)
        _wait_bytes(watch, (s1,), 2, 3.0)
        time.sleep(1.5)
        back, sent = watch.got(s1), watch.got(tap)
        dump = next((re.search(r"\[MAESTRO\] WCB2 broadcast  2 bytes: ([0-9A-F]{2}) ([0-9A-F]{2})", x)
                     for x in w.dev.since(wm) if "WCB2 broadcast" in x), None)
        if sent != GET_ERRORS_2:
            bad.append(f"W2 S1 should get exactly AA 02 21 (no re-bridged reply), got {sent.hex(' ') or 'nothing'}")
        if len(back) != 2:
            bad.append(f"W1 S1 should get exactly Maestro 2's 2-byte reply, got {back.hex(' ') or 'nothing'}")
        if not dump:
            bad.append("no '[MAESTRO] WCB2 broadcast  2 bytes' line on W1")
        elif len(back) == 2 and bytes.fromhex(dump.group(1) + dump.group(2)) != back:
            bad.append(f"the debug dump {dump.group(0)} does not match the bytes on W1 S1 {back.hex(' ')}")
        w.run("?DEBUG,MAESTRO,OFF")
        payload = bytes(range(1, 65))
        lossy = []
        for attempt in range(1, 4):
            watch = Watch(tap)   # fresh mark per try: expect() waits its full 4 s, so W1's queue has drained
            s1.send(payload)
            try:
                watch.expect(tap, payload, timeout=4)
                break
            except AssertionError:
                got = watch.got(tap)
                it = iter(payload)
                if not all(b in it for b in got):
                    bad.append(f"64 bytes, try {attempt}: W2 S1 got bytes out of order, duplicated or foreign - "
                               f"not frame loss: {got.hex(' ')}")
                    break
                lossy.append(64 - len(got))
                bench.note(f"64-byte burst try {attempt}: lost {64 - len(got)} byte(s) on the best-effort channel")
                time.sleep(0.5)
        else:
            bad.append(f"64 bytes: every one of 3 tries lost bytes ({lossy}) - far above the ~1 % frame loss")
        attempts, success, failed = _raw_stats(w)
        if attempts < 1 or failed:
            bad.append(f"raw bridging stats: attempts {attempts}, success {success}, failed {failed}")
    finally:
        w.run("?DEBUG,MAESTRO,OFF")
        _settle_maestro2(w)
    assert not bad, "; ".join(bad)


@test("kyber.config_negatives", "(should) ?KYBER parse errors leave the board as it was; a rejected port - out of range, software serial S3-S5, a local Maestro's port, or an explicit target that puts a local Maestro on the Kyber port (tracker #28) - does not switch RAM targeting on, rewrite the target table or add a slot", needs=["wcb1"], links=[])
def kyber_config_negatives(bench):
    """storeKyberSettings' 'S' branch sets kyberUseTargeting and then auto-populates kyberTargets[] from the Maestro
    slots, so every refusal has to come before it: the port range check, the ownership, soft-serial and local-Maestro
    refusals (kyberLocalPortRefused, WCB_Storage.cpp), and the scan of an explicit target list. The ownership and
    soft-serial refusals used to run after the auto-populate, so a refused ?KYBER,LOCAL,S3 printed 'Auto-populated' and
    left targeting on in RAM until reboot — invisible to ?backup on a Remote board, and a silent switch out of broadcast
    mode on a Kyber_Local one (kyber.local_rejected_port_keeps_forwarding). The dispatcher prints the settings block after
    every LOCAL/REMOTE/CLEAR, refused or not (WCB.ino:5405-5406).
    Greg's #28 decision (2026-09-22) also refuses the port of a local Maestro — accepting it moved the Maestro to 115200
    and KyberLocalTask echoed its bytes back into the same port — and a target that would put one there:
    ?KYBER,LOCAL,S2,M8:W1S2:57600 has no slot on S2 yet, so only the target scan catches it before the target loop
    creates one. Each of those two arms runs only when its precondition holds on W1 (a local Maestro and nothing else on
    S1; S2 free and no Maestro 8), and both run with WDP off: firmware that still accepts them makes W1 Kyber_Local and
    adds a local Maestro 8, which W2 and NaviCore would persist as a never-evicted proxy (module docstring). An accepted
    arm is undone with the full Kyber restore before WDP comes back."""
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    bad = []
    leaked = accepted = restored = False

    def arm(cmd, needles):
        nonlocal leaked, accepted
        out = w.run(cmd)
        bad.extend(_in_order(out, needles, cmd))
        accepted = accepted or _has(out, "Kyber is LOCAL on Serial")
        # The refusal must come before any live state changes: an auto-populate (or its 'no Maestro configs'
        # warning, which wipes the table too) or a parsed target means the table was rewritten first.
        touched = [x for x in out if re.search(r"auto-populate", x, re.I) or "Kyber target " in x]
        if touched:
            bad.append(f"the refused {cmd} rewrote the Kyber targets before refusing: {touched}")
        mode = [x for x in _kyber_list(w) if x.startswith("Targeting mode:")]
        if mode != ["Targeting mode: Disabled (Broadcast Mode)"]:
            leaked = True
            bad.append(f"after the refused {cmd}: {mode}")

    with config_guard(bench, 1) as before:
        # (command, needles, the port whose saved ?BAUD/?BCAST tokens it must leave alone)
        maestro_arms = []
        s1 = _port_devices(before[1], "S1")
        if s1 and all(t.upper().startswith("?MAESTRO,") for t in s1):
            maestro_arms.append(("?KYBER,LOCAL,S1", ["Cannot set Kyber LOCAL on Serial1 - a local Maestro is configured there",
                                                     "Kyber is Remote"], "S1"))
        else:
            bench.note("local-Maestro arm skipped: W1 S1 does not carry a local Maestro alone")
        if _port_devices(before[1], "S2") or [t for t in _m_lines(before[1]) if re.match(r"^\?MAESTRO,M8:", t, re.I)]:
            bench.note("explicit-target arm skipped: W1 S2 carries a device or W1 already has a Maestro 8")
        else:
            maestro_arms.append(("?KYBER,LOCAL,S2,M8:W1S2:57600",
                                 ["Cannot set Kyber LOCAL on Serial2 - target M8 puts a local Maestro on the Kyber port",
                                  "Kyber is Remote"], "S2"))
        try:
            for cmd, needles in (
                    ("?KYBER,LOCAL,X2", ["Invalid format. Use: ?KYBER,LOCAL,Sx or ?KYBER,LOCAL,Sx,M1:W1S1:57600", "Kyber is Remote"]),
                    ("?KYBER,LOCAL,S6", ["Invalid Kyber port. Must be S1-S5", "Kyber is Remote"]),
                    ("?KYBER,LOCAL,S0", ["Invalid Kyber port. Must be S1-S5", "Kyber is Remote"]),
                    ("?KYBER,LOCAL,S3", ["S3 is SOFTWARE SERIAL", "Kyber is Remote"]),
                    ("?KYBER,LOCAL,S4", ["S4 is SOFTWARE SERIAL", "Kyber is Remote"]),
                    ("?KYBER,LOCAL,S5,M1:W1S1:57600", ["S5 is SOFTWARE SERIAL", "Kyber is Remote"])):
                arm(cmd, needles)
            if maestro_arms:
                # WDP already off: nothing is advertised, and _wdp_off would refuse to toggle it.
                with nullcontext() if "?WDP,OFF" in before[1] else _wdp_off(w, before[1]):
                    try:
                        for cmd, needles, _ in maestro_arms:
                            arm(cmd, needles)
                        toks = snapshot(bench, 1)
                        for cmd, _, port in maestro_arms:
                            was, now = _port_tokens(before[1], port), _port_tokens(toks, port)
                            if was != now:
                                bad.append(f"after the refused {cmd} {port}'s saved settings are {now}, not {was}")
                        if any(",M8:" in cmd for cmd, _, _ in maestro_arms):
                            m8 = [x.rstrip() for x in w.run("?MAESTRO,LIST") if x.startswith("  Maestro 8 ")]
                            m8 += [t for t in toks if re.match(r"^\?MAESTRO,M8:", t, re.I)]
                            if m8:
                                bad.append(f"the refused explicit target still created a Maestro 8 slot: {m8}")
                    finally:
                        if accepted:    # undo it here, while WDP is still off, so a Maestro 8 is never advertised
                            if any(x.startswith("  Maestro 8 ") for x in w.run("?MAESTRO,LIST")):
                                w.run("?MAESTRO,CLEAR,M8:W1S2")
                            _restore_remote(w, _port_tokens(before[1], "S1") + _port_tokens(before[1], "S2"))
                            restored = True
        finally:
            if leaked or accepted:
                # An accepted arm left W1 Kyber_Local in NVS, so a bare reboot would boot it into that mode.
                if accepted and not restored:
                    _restore_remote(w, _port_tokens(before[1], "S1") + _port_tokens(before[1], "S2"))
                elif not restored:
                    w.reboot()
                if "Targeting mode: Disabled (Broadcast Mode)" not in _kyber_list(w):
                    bad.append("targeting was still on after a reboot")
    assert not bad, "; ".join(bad)


@test("kyber.local_mode_s2", "?KYBER,LOCAL,S2 on W1: the pre-reboot deaf window, targeted forwarding after reboot, Maestro-to-Kyber with no mesh copy, explicit targets, and the live switch to broadcast mode (two reboots)", needs=NEEDS)
def local_mode_s2(bench):
    """The tasks change only at boot (WCB.ino:8177-8184); the receive side reads Kyber_Local live (WCB.ino:4559-4567) and
    forwardDataFromKyber reads kyberUseTargeting on every call (WCB.ino:4724-4803): local targets get each byte, all
    remote targets share one broadcast. W1 advertises CAP K while local; W2 is already Maestro_Remote, so it does not
    flip. The copy-paste block printed for W2 is never sent — it relabels W2 S1 and reboots W2."""
    s1, s2, tap = link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 2, "S1")
    l1, _ = _require_remote_pair(bench)
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    b1 = _slot(l1, 1, 1)[1]
    proxy2 = next((int(m.group(1)) for m in (re.match(r"^\?MAESTRO,M2:W2S\d:(\d+)$", t, re.I) for t in l1) if m), None)
    bad = []
    with config_guard(bench, 1, 2) as before:
        ports = _port_tokens(before[1], "S2")
        try:
            w.run("?DEBUG,ON")
            out = w.run("?KYBER,LOCAL,S2", timeout=8)
            if _has(out, "Cannot set Kyber LOCAL"):
                raise Skip(f"W1 S2 is owned by another subsystem: {out}")
            bad += _in_order(out, ["Auto-populated", "Kyber is LOCAL on Serial2",
                                   "Reboot required — the Kyber forwarding task is only started at boot.",
                                   "Baud rate for Serial2 updated to 115200", "Set Serial2 to 115200 baud (Kyber standard)",
                                   "Kyber local with targeted forwarding configured", "--------Kyber Settings", "Kyber is Local",
                                   "Kyber port: Serial2 (115200 baud)", "------- Maestro Settings"], "LOCAL,S2")
            s1.listen()
            s2.listen(115200)
            tap.listen()
            t = marker("DEAF")
            watch, wm = Watch(s1, tap), w.dev.mark()
            s2.send(f";S0,{t}\r".encode())
            s2.send(GET_ERRORS_2)
            try:
                watch.silent(s1, tap, window=1.5)
            except AssertionError as e:
                bad.append(f"deaf window: S2 bytes were forwarded before the reboot: {e}")
            if _has(w.dev.since(wm), t) or _has(w.dev.since(wm), "Processing input from Serial2"):
                bad.append("deaf window: the serial parser still read S2")
            watch = Watch(s1, s2, tap)
            s1.send(GET_ERRORS_2)
            _wait_bytes(watch, (s1, s2), 2, 3.0)
            time.sleep(1.0)
            if watch.got(tap) != GET_ERRORS_2:
                bad.append(f"deaf window: the boot-time Maestro_Remote task should still bridge S1 once, W2 S1 got {watch.got(tap).hex(' ')}")
            if len(watch.got(s1)) != 2 or len(watch.got(s2)) != 2:
                bad.append(f"deaf window: the reply should go to W1 S1 and be echoed to S2, got {watch.got(s1).hex(' ')} / {watch.got(s2).hex(' ')}")
            else:
                bench.note("a Kyber_Local board writes inbound Maestro replies into its own Maestro ports too (WCB.ino:4561-4565); a reply byte >= 0x80 reaches them as a command byte")

            bm = w.reboot()
            boot = w.dev.since(bm)
            if not _has(boot, "Kyber_Local Task Created") or _has(boot, "Maestro_Remote Task Created"):
                bad.append("the reboot did not start the Kyber_Local task in place of Maestro_Remote")
            watch = Watch(s1, s2, tap)
            s2.send(GET_ERRORS_2)
            _wait_bytes(watch, (s2,), 2, 3.0)
            time.sleep(1.0)
            if watch.got(tap) != GET_ERRORS_2:
                bad.append(f"targeted: the remote targets should share one broadcast, W2 S1 got {watch.got(tap).hex(' ')}")
            if watch.got(s1)[:3] != GET_ERRORS_2 or len(watch.got(s1)) != 5:
                bad.append(f"targeted: W1 S1 should get AA 02 21 then the 2-byte reply, got {watch.got(s1).hex(' ')}")
            if len(watch.got(s2)) != 2:
                bad.append(f"targeted: the reply should be echoed to the Kyber port, got {watch.got(s2).hex(' ')}")
            watch = Watch(s2, tap)
            s1.send(b"\x12\x34")
            try:
                watch.expect(s2, b"\x12\x34", timeout=2)
            except AssertionError:
                bad.append(f"Maestro to Kyber: S2 got {watch.got(s2).hex(' ')}")
            time.sleep(1.5)
            if watch.got(tap):
                bad.append(f"Maestro to Kyber: a Kyber_Local board must not copy Maestro bytes to the mesh, W2 S1 got {watch.got(tap).hex(' ')}")

            if proxy2 is None:
                bench.note("explicit-target arm skipped: W1 has no M2:W2 proxy, and ?KYBER would create one")
            else:
                out = w.run(f"?KYBER,LOCAL,S2,M1:W1S1:{b1},M2:W2S1:{proxy2}", timeout=8)
                bad += _in_order(out, ["Kyber is LOCAL on Serial2", f"Kyber target 1: Maestro 1 → WCB1 S1 ({b1} baud)",
                                       f"✓ Maestro 1: Local S1 at {b1} baud (slot", f"Kyber target 2: Maestro 2 → WCB2 S1 ({proxy2} baud)",
                                       "✓ Maestro 2: Remote on WCB2", "COPY-PASTE SETUP COMMANDS FOR OTHER WCBs:", "WCB2 - Run this command:",
                                       f"?MAESTRO_REMOTE^?MAESTRO,M1:W1S1:{b1},M2:W2S1:{proxy2}^?SLS1,Maestro 2^?REBOOT",
                                       "Kyber local with targeted forwarding configured"], "explicit targets")
                targets = [x for x in _kyber_list(w) if re.match(r"^  Maestro \d → WCB\d+ S\d$", x)]
                if targets != ["  Maestro 1 → WCB1 S1", "  Maestro 2 → WCB2 S1"]:
                    bad.append(f"explicit targets listed {targets}")
                watch = Watch(s1, tap)
                s2.send(GET_ERRORS_2)
                for l in (s1, tap):
                    try:
                        watch.expect(l, GET_ERRORS_2, timeout=2)
                    except AssertionError:
                        bad.append(f"explicit targets: {l.key} never got AA 02 21")
                time.sleep(1.0)

            bad += _in_order(w.run("?KYBER,LOCAL", timeout=8), ["Kyber is LOCAL on Serial2", "Kyber local with broadcast mode"], "?KYBER,LOCAL")
            if "Targeting mode: Disabled (Broadcast Mode)" not in _kyber_list(w):
                bad.append("?KYBER,LOCAL did not return to broadcast mode")
            watch = Watch(s1, tap)
            s2.send(GET_ERRORS_2)
            for l in (s1, tap):
                try:
                    watch.expect(l, GET_ERRORS_2, timeout=2)
                except AssertionError:
                    bad.append(f"broadcast mode (live, no reboot): {l.key} never got AA 02 21")
            time.sleep(1.0)
        finally:
            _restore_remote(w, ports)
    assert not bad, "; ".join(bad)


@test("kyber.local_rejected_port_keeps_forwarding", "(should) On a Kyber_Local board in broadcast mode a rejected ?KYBER,LOCAL,S6 leaves forwarding working, instead of switching targeting on with an empty table that drops every Kyber byte until reboot; a refused ?KYBER,LOCAL,S3 leaves broadcast mode and the saved ?KYBER line alone", needs=NEEDS)
def local_rejected_port_keeps_forwarding(bench):
    """Tracker #6 / #28. A refusal that runs after the 'S' branch has set kyberUseTargeting (kyber.config_negatives)
    sends forwardDataFromKyber down its targeted branch (WCB.ino:4939): with the S6 range refusal the table is empty
    and every Kyber byte is dropped. The S3 soft-serial refusal also ran after the auto-populate, so the table is W1's
    Maestro slots and forwarding survives — that arm checks the mode and the ?backup line instead, which on a
    Kyber_Local board carries every enabled target (collectConfigCommands, WCB.ino). Only the ?KYBER,LOCAL line is compared: the
    backup also releases early with a ?KYBER,CLEAR ahead of the ?BAUD lines (#73 D5/D9, collectConfigCommands)."""
    s1, s2, tap = link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    bad = []
    with config_guard(bench, 1) as before:
        ports = _port_tokens(before[1], "S2")
        try:
            out = w.run("?KYBER,LOCAL", timeout=8)
            if _has(out, "Cannot set Kyber LOCAL"):
                raise Skip(f"W1 S2 is owned by another subsystem: {out}")
            w.reboot()
            s1.listen()
            s2.listen(115200)
            tap.listen()
            watch = Watch(s1)
            s2.send(GET_ERRORS_2)
            try:
                watch.expect(s1, GET_ERRORS_2, timeout=2)
            except AssertionError:
                raise AssertionError("control: Kyber_Local broadcast mode never forwarded S2 to W1 S1")
            time.sleep(1.5)
            if not _has(w.run("?KYBER,LOCAL,S6"), "Invalid Kyber port. Must be S1-S5"):
                bad.append("?KYBER,LOCAL,S6 was not refused")
            watch = Watch(s1, tap)
            s2.send(GET_ERRORS_2)
            for l in (s1, tap):
                try:
                    watch.expect(l, GET_ERRORS_2, timeout=2)
                except AssertionError:
                    bad.append(f"after the refused port {l.key} no longer receives Kyber bytes")
            time.sleep(1.0)
            out = w.run("?KYBER,LOCAL,S3", timeout=8)
            if not _has(out, "S3 is SOFTWARE SERIAL"):
                bad.append(f"?KYBER,LOCAL,S3 was not refused: {out}")
            if "Targeting mode: Disabled (Broadcast Mode)" not in _kyber_list(w):
                bad.append("the refused ?KYBER,LOCAL,S3 switched the board out of broadcast mode")
            kyber = [t for t in snapshot(bench, 1) if t.upper().startswith("?KYBER,LOCAL")]
            if kyber != ["?KYBER,LOCAL,S2"]:
                bad.append(f"after the refused ?KYBER,LOCAL,S3 the backup holds {kyber}, not ['?KYBER,LOCAL,S2']")
        finally:
            _restore_remote(w, ports)
    assert not bad, "; ".join(bad)


@test("kyber.local_port_s1_frees_s2", "(should) With the Kyber on S1 (Maestro moved off it), W1 S2 still has a command reader and still gets broadcasts; S1 has one reader", needs=NEEDS)
def local_port_s1_frees_s2(bench):
    """Tracker #14 (fixed), through the other hardware port. On a Kyber_Local board the Kyber owns only kyberLocalPort:
    serialCommandTask skips just that port (and raw-mapped ones), and processBroadcastCommand skips just that port plus
    the configured Maestro ports (both WCB.ino), so the other hardware port is a normal command port that follows its
    ?BCAST flags. Both used to skip S1 AND S2 whatever kyberLocalPort was, so with the Kyber on S1 and no Maestro on S2
    nothing read S2 and a saved ?BCAST,OUT,S2,ON was overridden. KyberLocalTask reads kyberLocalPort
    (forwardDataFromKyber) and the local Maestro ports (forwardMaestroDataToLocalKyber); the parser stays off a Maestro
    port a bridge task drains (maestroPortOwnedByKyberBridge). Maestro 1 is cleared off S1 BEFORE ?KYBER,LOCAL,S1,
    because ?KYBER,LOCAL refuses a local Maestro's port (#28, kyber.config_negatives). While W1 is Kyber_Local every byte
    into S1 is broadcast to W2's real Maestro 2 (and NaviCore), so only GET_ERRORS_2 goes into S1, never a CR flush. WDP
    is off for the slot rebuild (module docstring)."""
    s1, s2, s4, tap = link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 1, "S4"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,OUT,S4,ON")
    bad = []
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        # ?EPASS / ?WIFI are excluded so a password that happens to contain 'S2' never reaches the Skip message.
        owners = [t for t in before[1] if re.search(r"(?:\b|W1)S2\b", t)
                  and not re.match(r"^\?(BAUD|BCAST|LABEL|SLS|EPASS|WIFI)", t, re.I)]
        if owners:
            raise Skip(f"W1 S2 is configured for something else: {owners}")
        # Clearing M1 must free S1, and the auto-populated targets need a remote slot, or nothing the Kyber sends
        # reaches the mesh and the tap control below fails for a reason that is not the firmware's.
        shared = [t for t in lines if re.match(r"^\?MAESTRO,M[2-9]:W1S1:", t, re.I)]
        if shared:
            raise Skip(f"W1 S1 carries more Maestros than M1, so clearing M1 would not free it: {shared}")
        if all(re.match(r"^\?MAESTRO,M\d:W1S", t, re.I) for t in lines):
            raise Skip("W1 has no remote Maestro slot, so the Kyber's auto-populated targets would never reach the mesh")
        ports = _port_tokens(before[1], "S1") + _port_tokens(before[1], "S2")
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?MAESTRO,CLEAR,M1:W1S1")
                if not _has(out, "Cleared Maestro M1:W1S1"):
                    raise AssertionError(f"?MAESTRO,CLEAR,M1:W1S1 printed {out}")
                out = w.run("?KYBER,LOCAL,S1", timeout=8)
                if _has(out, "Cannot set Kyber LOCAL"):
                    raise Skip(f"W1 S1 is owned by another subsystem: {out}")
                bad += _in_order(out, ["Kyber is LOCAL on Serial1", "Reboot required", "Baud rate for Serial1 updated to 115200",
                                       "Set Serial1 to 115200 baud (Kyber standard)",
                                       "Kyber local with targeted forwarding configured"], "LOCAL,S1")
                bm = w.reboot()
                boot = w.dev.since(bm)
                if not _has(boot, "Kyber_Local Task Created") or _has(boot, "Maestro_Remote Task Created"):
                    raise AssertionError("the reboot did not start the Kyber_Local task in place of Maestro_Remote")
                w.run("?DEBUG,ON")
                s1.listen(115200)
                s2.listen()
                s4.listen()
                tap.listen()

                # Control: the Kyber owns S1. The parser logs a line only at its CR, which never goes into S1, so a
                # second reader would show mostly as a short copy on the tap; the log check catches the rest.
                watch, wm = Watch(s1, tap), w.dev.mark()
                s1.send(GET_ERRORS_2)
                try:
                    watch.expect(tap, GET_ERRORS_2, timeout=2)
                except AssertionError:
                    raise AssertionError(f"control: GET_ERRORS_2 from the Kyber on W1 S1 never reached W2 S1 whole, got {watch.got(tap).hex(' ') or 'nothing'}")
                time.sleep(1.0)
                parsed = [x for x in w.dev.since(wm)
                          if x.startswith("Processing input from Serial1:") or x.startswith("Broadcast blocked from Serial1")]
                if parsed:
                    bad.append(f"the serial parser also read the Kyber port S1: {parsed[:3]}")
                if GET_ERRORS_2 in watch.got(s1):
                    bad.append(f"the Kyber's own bytes came back out of W1 S1: {watch.got(s1).hex(' ')}")

                t4 = marker("S4")
                watch = Watch(s2)
                s4.send(f";S2{t4}\r".encode())
                try:
                    watch.expect(s2, t4.encode() + b"\r", timeout=3)
                except AssertionError:
                    raise AssertionError("control: a ;S2 command typed on W1 S4 never reached W1 S2")
                t2 = marker("S2")
                watch = Watch(s4)
                s2.send(f";S4{t2}\r".encode())
                time.sleep(2.0)
                if t2.encode() not in watch.got(s4):
                    bad.append("W1 S2 has no reader with the Kyber on S1: a ;S4 command typed there never ran")
                tb = marker("B")
                watch = Watch(s2, s4)
                w.send(tb)
                try:
                    watch.expect(s4, tb.encode() + b"\r", timeout=3)
                except AssertionError:
                    raise AssertionError("control: a USB broadcast never reached W1 S4")
                time.sleep(1.0)
                if tb.encode() not in watch.got(s2):
                    bad.append("broadcasts skip W1 S2 although ?BCAST,OUT,S2 is ON")
            finally:
                s2.send(b"\r")
                w.run("?KYBER,CLEAR", timeout=6)
                w.run("?MAESTRO,REMOTE", timeout=6)
                bad += _rebuild(w, lines)
                for t in ports:
                    w.run(t)
                bm = w.reboot()
                if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
                    bad.append("W1 did not boot with the Maestro_Remote task after the restore")
                w.run("?DEBUG,OFF")
                _settle_maestro2(w)
    assert not bad, "; ".join(bad)


def _clear_pwm_out(w, port):
    """?MAP,PWM,CLEAR,OUT,<port> and wait out its deferred reboot (s14's _inline_clear_out). Returns a problem or None.
    The reboot fires once the queue is quiet, so nothing may be sent until the board is back."""
    m = w.send(f"?MAP,PWM,CLEAR,OUT,{port}")
    try:
        w.dev.expect(r"^PWM output cleared", timeout=5, since=m)
        w.dev.expect(r"^Rebooting now to apply PWM configuration", timeout=30, since=m)
        w.wait_boot(m, timeout=30)
    except AssertionError as e:
        return f"clearing the PWM output on {port}: {e}"
    return None


@test("kyber.local_free_port_takes_pwm", "(should) With the Kyber on S1, W1 S2 takes a WLED and a PWM output like any port and boots with its UART off; the Kyber port S1 refuses both, and ?KYBER,LOCAL cannot move onto the PWM port (3 reboots)", needs=NEEDS)
def local_free_port_takes_pwm(bench):
    """Tracker #73 D4. A Kyber_Local board reserves only kyberLocalPort (kyberModeReservesPort, WCB_Storage.cpp) in
    the HCR/MP3/DFP/WLED guards and canUsePWMOnPort; both hardware ports used to be reserved, although since #14 the
    other one is a normal command port - and a PWM output saved there was dropped from NVS at the next boot. The boot
    init no longer begins that port's UART when a PWM input/output owns it (setup(), WCB.ino), so S2 must idle LOW:
    a guard change without the boot change would re-attach the UART and idle HIGH. Today's firmware fails first at
    the WLED S2 arm. Setup is local_port_s1_frees_s2's: Maestro 1 is cleared off S1 with WDP off (which also keeps
    peer PWMTARGET adverts out), and nothing is ever sent into S1 - while W1 is Kyber_Local every byte there is
    bridged to W2's real Maestro 2. S2 is a probe header only: the WLED and PWM arms put nothing on it but a LOW line
    and one pulse. Every refusal arm runs before any live change (kyberLocalPortRefused), and an arm the firmware
    wrongly accepts is undone in the finally."""
    s2 = link(bench, 1, "S2")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    bad = []
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        if any(t.upper().startswith("?MAP,PWM") for t in before[1]):
            raise Skip("W1 already has PWM configuration")
        # ?EPASS / ?WIFI are excluded so a password that happens to contain 'S2' never reaches the Skip message.
        owners = [t for t in before[1] if re.search(r"(?:\b|W1)S2\b", t)
                  and not re.match(r"^\?(BAUD|BCAST|LABEL|SLS|EPASS|WIFI)", t, re.I)]
        if owners:
            raise Skip(f"W1 S2 is configured for something else: {owners}")
        shared = [t for t in lines if re.match(r"^\?MAESTRO,M[2-9]:W1S1:", t, re.I)]
        if shared:
            raise Skip(f"W1 S1 carries more Maestros than M1, so clearing M1 would not free it: {shared}")
        wid = next((i for i in range(1, 10) if not any(t.upper().startswith(f"?WLED,{i}:") for t in before[1])), None)
        if wid is None:
            raise Skip("W1 has a WLED on every id 1-9")
        # WLED reserve/release rewrite the port's baud, BCAST flags and label (WCB_WLED.cpp wledReserveLocalPort).
        ports = (_port_tokens(before[1], "S1") + _port_tokens(before[1], "S2") +
                 [t for t in before[1] if re.match(r"^\?LABEL,S[12],", t, re.I)])
        s1_out = s2_out = False
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?MAESTRO,CLEAR,M1:W1S1")
                if not _has(out, "Cleared Maestro M1:W1S1"):
                    raise AssertionError(f"?MAESTRO,CLEAR,M1:W1S1 printed {out}")
                out = w.run("?KYBER,LOCAL,S1", timeout=8)
                if _has(out, "Cannot set Kyber LOCAL"):
                    raise Skip(f"W1 S1 is owned by another subsystem: {out}")

                # The guards read the live globals, so no reboot is needed for these. Stop at the first miss. The S1
                # refusals are matched by prefix: their wording differs between images ("Maestro/Kyber", "/DFP"), and
                # only the refusal itself is under test.
                out = w.run("?MAP,PWM,OUT,S1")
                s1_out = _has(out, "configured as PWM output port")
                if not _has(out, "Cannot use PWM on Serial1 - reserved for"):
                    raise AssertionError(f"?MAP,PWM,OUT,S1 on the Kyber port printed {out}")
                out = w.run(f"?WLED,{wid}:W1S1:115200")
                if not _has(out, "[WLED] S1 already in use by"):
                    raise AssertionError(f"?WLED,{wid}:W1S1 on the Kyber port printed {out}")
                out = w.run(f"?WLED,{wid}:W1S2:115200")
                if not _has(out, f"[WLED] WLED {wid}: local S2 at 115200 baud") or _has(out, "already in use"):
                    raise AssertionError(f"the free port S2 refused a WLED: {out}")
                out = w.run(f"?WLED,CLEAR,{wid}")
                if not _has(out, f"[WLED] WLED {wid} cleared"):
                    raise AssertionError(f"?WLED,CLEAR,{wid} printed {out}")
                out = w.run("?MAP,PWM,OUT,S2")
                s2_out = _has(out, "Serial2 configured as PWM output port")
                if not s2_out:
                    raise AssertionError(f"the free port S2 refused a PWM output: {out}")
                out = w.run("?KYBER,LOCAL,S2", timeout=8)
                if not _has(out, "Cannot set Kyber LOCAL on Serial2 - reserved by HCR/MP3/WLED/PWM/DFP"):
                    raise AssertionError(f"?KYBER,LOCAL,S2 moved the Kyber onto the PWM port: {out}")

                # Boot as Kyber_Local on S1 with the PWM output on S2: kept in NVS, and S2's UART never begun.
                bm = w.reboot()
                boot = w.dev.since(bm)
                if not _has(boot, "Kyber_Local Task Created"):
                    raise AssertionError("the reboot did not start the Kyber_Local task")
                if _has(boot, "Skipping PWM output port Serial2") or _has(boot, "Cannot use PWM on Serial2"):
                    bad.append("the boot dropped the PWM output on the free port S2")
                if not _has(boot, "Serial2 reserved for PWM - skipping UART init"):
                    bad.append("the boot began S2's UART although S2 is a PWM output")
                lst = [x.rstrip() for x in w.run("?MAP,PWM,LIST")]
                if "Configured outputs: S2" not in lst:
                    bad.append(f"after the reboot ?MAP,PWM,LIST lacks 'Configured outputs: S2': {lst}")
                if s2.line_level() != 0:
                    bad.append("W1 S2 does not idle LOW after the boot: its UART was begun over the PWM pin")
                s2.pwm_in()
                time.sleep(0.6)
                m = s2.probe.dev.mark()
                w.send(";P21500")
                time.sleep(0.6)
                got = s2.pulses(m)
                if len(got) != 1 or got[0][1] != 1 or abs(got[0][0] - 1500) > 40:
                    bad.append(f";P21500 on the free port S2 gave {got}, not one 1500 us pulse")
            finally:
                s2.pwm_stop()
                w.run(f"?WLED,CLEAR,{wid}")   # idempotent: "not configured" writes nothing
                # One deferred PWM reboot at a time, each waited out before anything else is sent.
                for port, was_set in (("S1", s1_out), ("S2", s2_out)):
                    if was_set:
                        problem = _clear_pwm_out(w, port)
                        if problem:
                            bad.append(problem)
                w.run("?KYBER,CLEAR", timeout=6)
                w.run("?MAESTRO,REMOTE", timeout=6)
                bad += _rebuild(w, lines)
                for t in ports:
                    w.run(t)
                bm = w.reboot()
                if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
                    bad.append("W1 did not boot with the Maestro_Remote task after the restore")
                w.run("?DEBUG,OFF")
                _settle_maestro2(w)
    assert not bad, "; ".join(bad)


def _usb_lines_equal(w, since, text, timeout=3.0, settle=1.0):
    """How many USB console lines since `since` are exactly `text` — waits up to `timeout` for the first, then
    `settle` more so a second (split or duplicated) copy has time to show."""
    try:
        w.dev.expect(rf"^{re.escape(text)}$", timeout=timeout, since=since)
    except AssertionError:
        pass
    time.sleep(settle)
    return sum(1 for x in w.dev.since(since) if x.strip() == text)


@test("kyber.local_port_move_releases_old", "(should) Moving the Kyber between S1 and S2 gives the old port back (9600, broadcasts in and out on), a bare ?KYBER,LOCAL keeps the current port, a live move on a Kyber_Local board takes effect without a reboot, and ?MAESTRO,REMOTE releases the port and stops the Kyber task reading it (two reboots)", needs=NEEDS)
def local_port_move_releases_old(bench):
    """Tracker #73 D5/D9. Every way out of Kyber LOCAL releases the port through kyberReleasePort (WCB_Storage.cpp): a
    ?KYBER,LOCAL move and ?MAESTRO,REMOTE used to leave the old port at 115200 with broadcasts off, and the bare
    ?KYBER,LOCAL hard-coded S2, re-pointing a Kyber on S1. REMOTE never zeroed kyberLocalPort, which KyberLocalTask
    gates on alone (forwardDataFromKyber, WCB.ino), so a Kyber_Local-booted board kept draining the old Kyber port
    beside the Maestro_Remote parser branch (serialCommandTask, WCB.ino) until reboot. Setup is local_port_s1_frees_s2's: Maestro 1
    is cleared off S1 with WDP off, and while a port is the Kyber's only GET_ERRORS_2 goes into it. ASCII ;S0 lines go
    into a port only after its release has printed and the wire is re-bound at 9600, so older firmware (which fails at
    step b, before any injection) never has mis-baud bytes bridged into W2's Maestro. The ;S0 checks read USB, not a
    W1 S4 wire (tracker #78)."""
    s1, s2, tap = link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    released_s2 = ["?BAUD,S2,9600", "?BCAST,OUT,S2,ON", "?BCAST,IN,S2,ON"]
    bad = []
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        # ?EPASS / ?WIFI are excluded so a password that happens to contain 'S2' never reaches the Skip message.
        owners = [t for t in before[1] if re.search(r"(?:\b|W1)S2\b", t)
                  and not re.match(r"^\?(BAUD|BCAST|LABEL|SLS|EPASS|WIFI)", t, re.I)]
        if owners:
            raise Skip(f"W1 S2 is configured for something else: {owners}")
        shared = [t for t in lines if re.match(r"^\?MAESTRO,M[2-9]:W1S1:", t, re.I)]
        if shared:
            raise Skip(f"W1 S1 carries more Maestros than M1, so clearing M1 would not free it: {shared}")
        if all(re.match(r"^\?MAESTRO,M\d:W1S", t, re.I) for t in lines):
            raise Skip("W1 has no remote Maestro slot, so the Kyber's auto-populated targets would never reach the mesh")
        ports = _port_tokens(before[1], "S1") + _port_tokens(before[1], "S2")
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?MAESTRO,CLEAR,M1:W1S1")
                if not _has(out, "Cleared Maestro M1:W1S1"):
                    raise AssertionError(f"?MAESTRO,CLEAR,M1:W1S1 printed {out}")

                # (a) Kyber on S2.
                out = w.run("?KYBER,LOCAL,S2", timeout=8)
                if _has(out, "Cannot set Kyber LOCAL"):
                    raise Skip(f"W1 S2 is owned by another subsystem: {out}")
                problems = _in_order(out, ["Kyber is LOCAL on Serial2", "Set Serial2 to 115200 baud (Kyber standard)"], "LOCAL,S2")
                if problems:
                    raise AssertionError(problems[0])

                # (b) Move it to S1 (not yet a Kyber_Local boot): S2 is given back. Stop at the first miss.
                out = w.run("?KYBER,LOCAL,S1", timeout=8)
                if _has(out, "Cannot set Kyber LOCAL"):
                    raise Skip(f"W1 S1 is owned by another subsystem: {out}")
                problems = _in_order(out, ["Kyber is LOCAL on Serial1", "Set Serial1 to 115200 baud (Kyber standard)",
                                           "Reset S2 baud rate to 9600 (was the Kyber port)"], "move S2 -> S1")
                if problems:
                    raise AssertionError(problems[0])
                toks = snapshot(bench, 1)
                missing = [t for t in released_s2 if t not in toks]
                if missing:
                    raise AssertionError(f"after the move S2 is not released in ?backup: missing {missing}")

                # (c) Bare ?KYBER,LOCAL keeps the Kyber on S1 and only switches to broadcast mode.
                out = w.run("?KYBER,LOCAL", timeout=8)
                problems = _in_order(out, ["Kyber is LOCAL on Serial1", "Kyber local with broadcast mode"], "bare LOCAL")
                if problems:
                    raise AssertionError(problems[0])
                if _has(out, "Set Serial2 to 115200"):
                    raise AssertionError(f"the bare ?KYBER,LOCAL re-claimed S2: {out}")
                toks = snapshot(bench, 1)
                kyber = token(toks, "?KYBER,LOCAL") or ""
                if not kyber.upper().startswith("?KYBER,LOCAL,S1"):
                    raise AssertionError(f"after the bare ?KYBER,LOCAL the backup's Kyber line is {kyber!r}, not ?KYBER,LOCAL,S1")
                missing = [t for t in released_s2 if t not in toks]
                if missing:
                    raise AssertionError(f"the bare ?KYBER,LOCAL undid S2's release: missing {missing}")

                # (d) Boot as Kyber_Local on S1. Control: the Kyber's bytes reach W2's Maestro.
                bm = w.reboot()
                boot = w.dev.since(bm)
                if not _has(boot, "Kyber_Local Task Created") or _has(boot, "Maestro_Remote Task Created"):
                    raise AssertionError("the reboot did not start the Kyber_Local task in place of Maestro_Remote")
                s1.listen(115200)
                s2.listen(9600)
                tap.listen()
                watch = Watch(tap)
                s1.send(GET_ERRORS_2)
                try:
                    watch.expect(tap, GET_ERRORS_2, timeout=2)
                except AssertionError:
                    raise AssertionError(f"control: GET_ERRORS_2 from the Kyber on W1 S1 never reached W2 S1 whole, got {watch.got(tap).hex(' ') or 'nothing'}")
                time.sleep(1.0)

                # (e) Live move back to S2 on the running KyberLocalTask: S1 is released, the task follows.
                out = w.run("?KYBER,LOCAL,S2", timeout=8)
                problems = _in_order(out, ["Kyber is LOCAL on Serial2", "Set Serial2 to 115200 baud (Kyber standard)",
                                           "Reset S1 baud rate to 9600 (was the Kyber port)"], "live move S1 -> S2")
                if problems:
                    raise AssertionError(problems[0])   # no release: never type ASCII into S1
                s1.listen(9600)
                s2.listen(115200)
                watch = Watch(tap)
                s2.send(GET_ERRORS_2)
                try:
                    watch.expect(tap, GET_ERRORS_2, timeout=2)
                except AssertionError:
                    bad.append(f"after the live move the Kyber task did not follow to S2 without a reboot: W2 S1 got {watch.got(tap).hex(' ') or 'nothing'}")
                time.sleep(1.0)
                m1 = marker("MV")
                wm, watch = w.dev.mark(), Watch(tap)
                s1.send(f";S0,{m1}\r".encode())
                n = _usb_lines_equal(w, wm, m1)
                if n != 1:
                    bad.append(f"the released S1 should be a command port with one reader: ';S0,{m1}' printed {n} whole line(s)")
                if watch.got(tap):
                    bad.append(f"bytes typed into the released S1 still reached the mesh: W2 S1 got {watch.got(tap).hex(' ')}")

                # (f) ?MAESTRO,REMOTE releases S2 and stops the Kyber task reading it before any reboot (D9).
                out = w.run("?MAESTRO,REMOTE", timeout=8)
                problems = _in_order(out, ["Kyber is REMOTE (on another WCB)", "Reboot required",
                                           "Reset S2 baud rate to 9600 (was the Kyber port)"], "?MAESTRO,REMOTE")
                if problems:
                    raise AssertionError(problems[0])   # no release: never type ASCII into S2
                s2.listen(9600)
                m2 = marker("RM")
                wm, watch = w.dev.mark(), Watch(tap)
                s2.send(f";S0,{m2}\r".encode())
                n = _usb_lines_equal(w, wm, m2, settle=2.0)
                if n != 1:
                    bad.append(f"after ?MAESTRO,REMOTE S2 should have one reader (the parser): ';S0,{m2}' printed {n} whole line(s)")
                if watch.got(tap):
                    bad.append(f"after ?MAESTRO,REMOTE the Kyber task still drained S2 into the mesh: W2 S1 got {watch.got(tap).hex(' ')}")
                toks = snapshot(bench, 1)
                missing = [t for t in released_s2 + ["?MAESTRO,REMOTE"] if t not in toks]
                if missing:
                    bad.append(f"after ?MAESTRO,REMOTE the backup lacks {missing}")
                elif toks.index("?MAESTRO,REMOTE") > next((i for i, t in enumerate(toks) if t.startswith("?BAUD,")), len(toks)):
                    bad.append("?backup emits ?MAESTRO,REMOTE after the ?BAUD lines, so a replayed release would undo them")
            finally:
                w.run("?KYBER,CLEAR", timeout=6)
                w.run("?MAESTRO,REMOTE", timeout=6)
                bad += _rebuild(w, lines)
                for t in ports:
                    w.run(t)
                bm = w.reboot()
                if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
                    bad.append("W1 did not boot with the Maestro_Remote task after the restore")
                _settle_maestro2(w)
    assert not bad, "; ".join(bad)


def _w2_online(w, wait=30):
    """True once W1's ?STATS shows WCB2 online (it is back at W2's next heartbeat after W1 reboots), False after
    `wait` s. A board W1 still thinks offline gets no expected-ACK slot, which muddies an ACK check."""
    deadline = time.monotonic() + wait
    while not any(x.startswith("WCB2: ") and "Online" in x for x in w.run("?STATS")):
        if time.monotonic() > deadline:
            return False
        time.sleep(2)
    return True


@test("kyber.local_s1_legacy_fallback_skips_kyber_port", "(should) With the Kyber on S1 and no Maestro slot for W1's own id, the legacy S1 fallbacks (;M<own id>, ;M9, the ;M0 extra frame, a get on the own id) write nothing into the Kyber's port and never self-forward; ?KYBER and ?config name the real Kyber port", needs=NEEDS)
def local_s1_legacy_fallback_skips_kyber_port(bench):
    """Tracker #73 D6 + D8. With no slot for its own number a board treats a Maestro on S1 with device id = its WCB
    number as its own (WCB_Maestro.cpp sendMaestroCommand / sendMaestroServoVerb / handleMaestroGet). Those writes had
    no ownership check, so they put 0xAA frames into whatever owns S1 - here the Kyber - and a get on the own id read
    its "reply" out of the Kyber's stream. legacyS1Owner() now gates all seven sites. ;M0 still goes to the mesh: only
    its extra local S1 frame is skipped. ?MAESTRO,CLEAR,M1 clears every M1 slot (including a NaviCore proxy), so ;M1
    reaches the legacy branch. The subroutine arms (;M95, ;M12) never leave W1 - the target-9 and own-id branches do
    not forward - so even on firmware without the gate they reach only the S1 probe. D8: printKyberSettings prints the
    real port and baud, and printBaudRates' auto label follows kyberLocalPort; W1 labels every port, so its S1/S2
    labels are cleared for one ?config and replayed from its own tokens. Setup and restore mirror
    kyber.local_port_s1_frees_s2; WDP is off for the slot rebuild (module docstring)."""
    s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    _require_remote_pair(bench)          # W1 is WCB1: the arms use ;M12, AA01 and "Maestro 1" literally
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    bad = []
    with config_guard(bench, 1, 2) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        # ?EPASS / ?WIFI are excluded so a password that happens to contain 'S2' never reaches the Skip message.
        owners = [t for t in before[1] if re.search(r"(?:\b|W1)S2\b", t)
                  and not re.match(r"^\?(BAUD|BCAST|LABEL|SLS|EPASS|WIFI)", t, re.I)]
        if owners:
            raise Skip(f"W1 S2 is configured for something else: {owners}")
        others = [t for t in lines if re.match(r"^\?MAESTRO,M[2-9]:W1S", t, re.I)]
        if others:
            raise Skip(f"W1 has a local Maestro other than M1, so ;M9 would not take the legacy S1 branch: {others}")
        if all(re.match(r"^\?MAESTRO,M1:", t, re.I) for t in lines):
            raise Skip("W1 has no Maestro slot other than M1, so the Kyber's auto-populated targets would be empty")
        labels = [t for t in before[1] if re.match(r"^\?LABEL,S[12],", t, re.I)]
        ports = _port_tokens(before[1], "S1") + _port_tokens(before[1], "S2")
        bench.note("restore if aborted: ?KYBER,CLEAR, ?MAESTRO,REMOTE, ?WDP,OFF, ?MAESTRO,CLEAR,ALL, then "
                   + " , ".join(lines + ports + labels) + " , ?WDP,ON, ?reboot")
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?MAESTRO,CLEAR,M1")
                if not _has(out, "Cleared Maestro ID 1"):
                    raise AssertionError(f"?MAESTRO,CLEAR,M1 printed {out}")
                out = w.run("?KYBER,LOCAL,S1", timeout=8)
                if _has(out, "Cannot set Kyber LOCAL"):
                    raise Skip(f"W1 S1 is owned by another subsystem: {out}")
                bad += _in_order(out, ["Kyber is LOCAL on Serial1", "--------Kyber Settings", "Kyber is Local",
                                       "Kyber port: Serial1 (115200 baud)"], "LOCAL,S1")
                if _has(out, "Initialized Serial1"):
                    bad.append("?KYBER still prints the hard-coded 'Initialized Serial1 & Serial2' line")
                bm = w.reboot()
                boot = w.dev.since(bm)
                if not _has(boot, "Kyber_Local Task Created") or _has(boot, "Maestro_Remote Task Created"):
                    raise AssertionError("the reboot did not start the Kyber_Local task in place of Maestro_Remote")
                if not _has(boot, "Kyber port: Serial1 (115200 baud)"):
                    bad.append("the boot banner does not name the Kyber port as Serial1 (115200 baud)")
                for kind in ("", "MAESTRO,", "ETM,"):
                    w.run(f"?DEBUG,{kind}ON")
                s1.listen(115200)
                tap.listen()
                s1.probe.rule_clear()
                w.run("?VAR,CLEAR,m1err")
                if not _w2_online(w):
                    bench.note("W1's ?STATS never showed WCB2 online after the reboot; the ;M0 ACK check may fail for that")

                watch, sent = Watch(s1), []
                for cmd, line in (
                        (";M95", "→ Maestro (local, target 9): no local Maestro, and S1 is the Kyber port - not sent, Script 5"),
                        (";M9,stopScript", "→ Maestro (local, target 9) verb '9,stopScript': no local Maestro, and S1 is the Kyber port - not sent"),
                        (";M12", "→ Maestro 1: no slot, and S1 is the Kyber port - not sent"),
                        (";M1,stopScript", "→ Maestro 1 verb '1,stopScript': no slot, and S1 is the Kyber port - not sent"),
                        (";M1,getErrors", "[MAESTRO] get 1 'getErrors': no slot, and S1 is the Kyber port - not sent")):
                    wm = w.dev.mark()
                    w.send(cmd)
                    time.sleep(1.0)
                    got = w.dev.since(wm)
                    sent += got
                    if not _has(got, line):
                        bad.append(f"{cmd}: no '{line}'")
                if _get(w, "m1err") != _not_set("m1err"):
                    bad.append(f"the own-id get read a reply out of the Kyber port: {_get(w, 'm1err')}")

                # Control: ;M0 still broadcasts - only its extra local S1 frame is skipped.
                mt, wm = tap.mark(), w.dev.mark()
                w.send(";M0,stopScript")
                problem = _exact(tap, mt, bytes.fromhex("AA0224"), settle=1.8)
                if problem:
                    bad.append(f";M0,stopScript: {problem}")
                got = w.dev.since(wm)
                sent += got
                if not _has(got, "→ Maestro Broadcast verb '0,stopScript': legacy S1 frame skipped - S1 is the Kyber port"):
                    bad.append(";M0,stopScript: no 'legacy S1 frame skipped - S1 is the Kyber port' line")
                seqs = _sent_seq(got, ";M0,stopScript")
                if len(seqs) != 1 or not _acked_by(got, 2, seqs[0]):
                    bad.append(f";M0,stopScript: expected one '[ETM] Sent seq N: ;M0,stopScript' ACKed by WCB2, got {seqs}")

                try:
                    watch.silent(s1, window=1.5)
                except AssertionError as e:
                    bad.append(f"a legacy fallback wrote into the Kyber port S1: {e}")
                forwards = [x for x in sent if x.startswith("[ETM] Sent seq") and ": ;M" in x and ": ;M0," not in x]
                if forwards:
                    bad.append(f"a legacy command went to the mesh (a board cannot unicast itself): {forwards}")

                # D8: with no user label the Kyber's row says so, and only that row.
                try:
                    for p in ("S1", "S2"):
                        w.run(f"?LABEL,CLEAR,{p}")
                    cfg = [x.rstrip() for x in w.run("?config")]
                    row1 = next((x.strip() for x in cfg if x.strip().startswith("Serial1 Baud:")), "")
                    row2 = next((x.strip() for x in cfg if x.strip().startswith("Serial2 Baud:")), "")
                    if not row1.startswith("Serial1 Baud: 115200,") or not row1.endswith("(Kyber)"):
                        bad.append(f"?config: the Kyber's row is not labelled (Kyber): '{row1}'")
                    if not row2 or "(Kyber)" in row2:
                        bad.append(f"?config: the Serial2 row is missing or says (Kyber): '{row2}'")
                finally:
                    for t in labels:
                        w.run(t)
            finally:
                s1.probe.rule_clear()
                w.run("?VAR,CLEAR,m1err")
                w.run("?KYBER,CLEAR", timeout=6)
                w.run("?MAESTRO,REMOTE", timeout=6)
                bad += _rebuild(w, lines)
                for t in ports:
                    w.run(t)
                bm = w.reboot()
                if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
                    bad.append("W1 did not boot with the Maestro_Remote task after the restore")
                for kind in ("", "MAESTRO,", "ETM,"):
                    w.run(f"?DEBUG,{kind}OFF")
                _settle_maestro2(w)
    assert not bad, "; ".join(bad)


@test("kyber.local_clear_maestro_drops_target", "(should) On a Kyber_Local board in targeted mode ?MAESTRO,CLEAR of a local Maestro drops its Kyber target, so the Kyber stops writing the freed port and ?backup no longer re-creates the slot; the freed port follows its ?BCAST flags; re-adding the Maestro restores the target", needs=NEEDS)
def local_clear_maestro_drops_target(bench):
    """Tracker #73 D7. forwardDataFromKyber writes every enabled local kyberTargets[] port whether or not a Maestro slot
    is still there, and ?backup's KYBER,LOCAL target list re-creates a slot for each target on restore
    (storeKyberSettings). So a cleared Maestro left the Kyber writing a port the clear had handed back (at the 9600 it
    forced), and a restore brought the Maestro back. _clearMaestroSlot now drops the (id, W1, port) target and a newly
    claimed ?MAESTRO slot adds it. Deliberately NOT a broadcast skip keyed on Kyber targets (#14's rule: ?BCAST flags
    decide): the USB broadcast arm locks that in. ;M9 then takes the legacy S1 branch on the freed, unowned S1 - the
    D6 gate must not over-block it. S1 is a probe wire; the only real Maestro (W2's M2) receives only getErrors and
    stopScript frames. WDP is off around every slot change (module docstring)."""
    s1, s2, tap = link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 2, "S1")
    l1, _ = _require_remote_pair(bench)  # W1 is WCB1 with M1 on S1
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    b1 = _slot(l1, 1, 1)[1]
    bad = []
    with config_guard(bench, 1, 2) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        # ;M9 takes the legacy S1 branch only with no local Maestro left, and the drop frees S1 only when M1 was the
        # last slot on it - the same checks maestro.clear_all_legacy_routing and kyber.local_port_s1_frees_s2 make.
        if [t for t in lines if re.match(r"^\?MAESTRO,M\d:W1S[2-5]:", t, re.I)]:
            raise Skip("W1 has a local Maestro off S1, so ;M9 would not take the legacy S1 branch")
        shared = [t for t in lines if re.match(r"^\?MAESTRO,M[2-9]:W1S1:", t, re.I)]
        if shared:
            raise Skip(f"W1 S1 carries more Maestros than M1, so clearing M1 would not free it: {shared}")
        if all(re.match(r"^\?MAESTRO,M\d:W1S", t, re.I) for t in lines):
            raise Skip("W1 has no remote Maestro slot, so the Kyber's auto-populated targets would never reach the mesh")
        ports = _port_tokens(before[1], "S1") + _port_tokens(before[1], "S2")
        bench.note("restore if aborted: ?KYBER,CLEAR, ?MAESTRO,REMOTE, ?WDP,OFF, ?MAESTRO,CLEAR,ALL, then "
                   + " , ".join(lines + ports) + " , ?WDP,ON, ?reboot")
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?KYBER,LOCAL,S2", timeout=8)
                if _has(out, "Cannot set Kyber LOCAL"):
                    raise Skip(f"W1 S2 is owned by another subsystem: {out}")
                bad += _in_order(out, ["Auto-populated", "Kyber is LOCAL on Serial2", "Kyber port: Serial2 (115200 baud)"], "LOCAL,S2")
                bm = w.reboot()
                boot = w.dev.since(bm)
                if not _has(boot, "Kyber_Local Task Created") or _has(boot, "Maestro_Remote Task Created"):
                    raise AssertionError("the reboot did not start the Kyber_Local task in place of Maestro_Remote")
                if "  Maestro 1 → WCB1 S1" not in _kyber_list(w):
                    raise AssertionError(f"control: the auto-populated targets have no Maestro 1 → WCB1 S1: {_kyber_list(w)}")
                s1.listen(b1)
                s2.listen(115200)
                tap.listen()
                s1.probe.rule_clear()

                # Control: the Kyber's bytes reach the local target and the remote one.
                watch = Watch(s1, tap)
                s2.send(GET_ERRORS_2)
                for l in (s1, tap):
                    try:
                        watch.expect(l, GET_ERRORS_2, timeout=2)
                    except AssertionError:
                        raise AssertionError(f"control: {l.key} never got the Kyber's AA 02 21, got {watch.got(l).hex(' ') or 'nothing'}")
                time.sleep(1.0)

                out = w.run("?MAESTRO,CLEAR,M1:W1S1")
                bad += _in_order(out, ["✓ Reset S1 baud rate to 9600", "✓ Removed Kyber target M1 → WCB1 S1",
                                       "Cleared Maestro M1:W1S1"], "CLEAR,M1:W1S1")
                if "  Maestro 1 → WCB1 S1" in _kyber_list(w):
                    bad.append("?KYBER,LIST still holds Maestro 1 → WCB1 S1 after its slot was cleared")
                kyber = token(snapshot(bench, 1), "?KYBER,LOCAL") or ""
                if not kyber.upper().startswith("?KYBER,LOCAL,S2") or ",M1:W1S1:" in kyber.upper():
                    bad.append(f"?backup's Kyber line would re-create the cleared Maestro on restore: '{kyber}'")

                # The freed S1 (now 9600): no Kyber bytes, but it does follow its ?BCAST flags.
                s1.listen(9600)
                watch = Watch(s1, tap)
                s2.send(GET_ERRORS_2)
                try:
                    watch.expect(tap, GET_ERRORS_2, timeout=2)
                except AssertionError:
                    bad.append(f"after the clear the remote target lost the Kyber's bytes, W2 S1 got {watch.got(tap).hex(' ') or 'nothing'}")
                try:
                    watch.silent(s1, window=1.5)
                except AssertionError as e:
                    bad.append(f"the Kyber still writes the freed port S1: {e}")
                tb = marker("B")
                watch = Watch(s1)
                w.send(tb)
                try:
                    watch.expect(s1, tb.encode() + b"\r", timeout=3)
                except AssertionError:
                    bad.append("a USB broadcast skipped the freed W1 S1, whose ?BCAST,OUT the clear re-enabled")
                time.sleep(0.5)
                m1 = s1.mark()
                w.send(";M9,stopScript")
                problem = _exact(s1, m1, bytes.fromhex("AA0124"), settle=0.8)
                if problem:
                    bad.append(f";M9,stopScript on the free S1 (the D6 gate must not block it): {problem}")

                out = w.run(f"?MAESTRO,M1:W1S1:{b1}")
                if not _has(out, "✓ Kyber target added: Maestro 1 → WCB1 S1"):
                    bad.append(f"re-adding M1:W1S1 did not restore its Kyber target: {out}")
                s1.listen(b1)
                watch = Watch(s1)
                s2.send(GET_ERRORS_2)
                try:
                    watch.expect(s1, GET_ERRORS_2, timeout=2)
                except AssertionError:
                    bad.append(f"after the re-add W1 S1 never got the Kyber's AA 02 21, got {watch.got(s1).hex(' ') or 'nothing'}")
                time.sleep(1.0)
            finally:
                s1.probe.rule_clear()
                w.run("?KYBER,CLEAR", timeout=6)
                w.run("?MAESTRO,REMOTE", timeout=6)
                bad += _rebuild(w, lines)
                for t in ports:
                    w.run(t)
                bm = w.reboot()
                if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
                    bad.append("W1 did not boot with the Maestro_Remote task after the restore")
                w.run("?DEBUG,OFF")
                _settle_maestro2(w)
    assert not bad, "; ".join(bad)


@test("kyber.clear_warns_reboot_single_reader", "(should) ?KYBER,CLEAR on a Maestro_Remote board says a reboot is needed, and until then S1 is not read by both the serial parser and the still-running Kyber task", needs=NEEDS)
def clear_warns_reboot_single_reader(bench):
    """Tracker #59. The Kyber tasks are created in setup() and never deleted, so after CLEAR the serial parser reads S1
    again while KyberRemoteTask is still running: forwardMaestroDataToRemoteKyber must idle once neither Kyber mode is
    live, or the two readers split every frame. CLEAR on a board that was in a Kyber mode prints the same reboot notice
    as LOCAL and REMOTE. WDP is off so an advert cannot flip W1 back mid-test (kyber.wdp_auto_remote_revert). Bytes are
    [0-9A-Z] only: S1 input stays blocked, nothing executes."""
    s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    bad = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?KYBER,CLEAR", timeout=6)
                bad += _in_order(out, ["Kyber cleared. Run ?MAESTRO_DEFAULT to clear Maestro configs.", "Kyber is Not used"], "?KYBER,CLEAR")
                if not _has(out, "Reboot required"):
                    bad.append("?KYBER,CLEAR printed no reboot notice, though the Kyber tasks only change at boot")
                s1.listen()
                tap.listen()
                w.run("?STATS,RESET")
                w.run("?DEBUG,ON")
                wm, watch = w.dev.mark(), Watch(tap)
                for _ in range(20):
                    s1.send(padded("DUAL", 36).encode() + b"\r")
                    time.sleep(0.05)
                time.sleep(1.5)
                parsed = [x for x in w.dev.since(wm) if x.startswith("Processing input from Serial1:")]
                bridged = len(watch.got(tap))
                bench.note(f"after CLEAR: {len(parsed)} S1 lines reached the parser and {bridged} bytes "
                           f"({_raw_stats(w)[0]} raw sends) reached W2 S1, of {37 * 20} injected")
                if parsed and bridged:
                    bad.append(f"S1 has two readers: {len(parsed)} fragments parsed and {bridged} bytes bridged")
            finally:
                s1.send(b"\r")
                w.run("?MAESTRO,REMOTE", timeout=6)
                bm = w.reboot()
                if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
                    bad.append("W1 did not boot with the Maestro_Remote task after the restore")
                w.run("?DEBUG,OFF")
                _settle_maestro2(w)
    assert not bad, "; ".join(bad)


@test("kyber.maestro_s2_single_reader", "(should) A local Maestro on S2 of a Maestro_Remote board has one reader: its bytes go to the Kyber bridge, not also to the serial parser", needs=NEEDS)
def maestro_s2_single_reader(bench):
    """Tracker #59. KyberRemoteTask drains every local Maestro port, not a fixed one, so processIncomingSerial must skip
    such a port while a bridge task reads it (maestroPortOwnedByKyberBridge), or the parser takes a share of each Maestro
    frame. A local Maestro on S3-S5 follows the same rule. WDP goes off before the add, so the new local id is never
    advertised and W2 never persists a never-evicted proxy for it."""
    s2, tap = link(bench, 1, "S2"), link(bench, 2, "S1")
    _require_remote_pair(bench)
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1, 2) as before:
        lines = _m_lines(before[1])
        if len(lines) > 8 or any(t.upper().startswith("?MAESTRO,M5:") for t in lines):
            raise Skip("W1 needs a free slot and no Maestro 5")
        # ?EPASS / ?WIFI are excluded so a password that happens to contain 'S2' never reaches the Skip message.
        owners = [t for t in before[1] if re.search(r"(?:\b|W1)S2\b", t)
                  and not re.match(r"^\?(BAUD|BCAST|LABEL|SLS|EPASS|WIFI)", t, re.I)]
        if owners:
            raise Skip(f"W1 S2 is configured for something else: {owners}")
        ports = _port_tokens(before[1], "S2")
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?MAESTRO,M5:W1S2:9600")
                if not _has(out, "✓ Maestro 5: Local S2 at 9600 baud (slot"):
                    raise AssertionError(f"?MAESTRO,M5:W1S2:9600 printed {out}")
                s2.listen(9600)
                tap.listen()
                w.run("?STATS,RESET")
                w.run("?DEBUG,ON")
                wm, watch = w.dev.mark(), Watch(tap)
                for _ in range(20):
                    s2.send(padded("MS2", 36).encode() + b"\r")
                    time.sleep(0.1)
                time.sleep(1.5)
                parsed = [x for x in w.dev.since(wm)
                          if x.startswith("Processing input from Serial2:") or x.startswith("Broadcast blocked from Serial2")]
                bench.note(f"Maestro on S2: {len(parsed)} parser lines, {len(watch.got(tap))} bytes "
                           f"({_raw_stats(w)[0]} raw sends) bridged to W2 S1, of {37 * 20} injected")
                if parsed:
                    bad.append(f"the serial parser read the Maestro port: {parsed[:3]}")
                m, wm = s2.mark(), w.dev.mark()
                w.send(";M51")
                problem = _exact(s2, m, bytes.fromhex("AA052701"))
                if problem or not _has(w.dev.since(wm), "→ Maestro 5: Local S2, Script 1"):
                    bad.append(f";M51 on the S2 Maestro: {problem or 'no route line'}")
            finally:
                s2.send(b"\r")
                w.run("?MAESTRO,CLEAR,M5:W1S2")
                for t in ports:
                    w.run(t)
                w.run("?DEBUG,OFF")
                _settle_maestro2(w)
    assert not bad, "; ".join(bad)


@test("kyber.target_id9_rejected", "(should) ?KYBER refuses Maestro id 9 as ?MAESTRO does, so ;M9... still reaches each local Maestro instead of a stored device 9", needs=NEEDS)
def target_id9_rejected(bench):
    """storeKyberSettings takes target ids 1-9 (WCB_Storage.cpp:1313) while ?MAESTRO, both clears and WDP auto-add take 1-8, and CLAUDE.md rule 5
    says 9 is never stored. A slot with id 9 matches in the config loop before the target-9 branch (WCB_Maestro.cpp:108-158
    before :190), so ;M95 goes out as device 9, and neither clear form can remove it (:817, :844). Restored by a WDP-off
    rebuild and ?MAESTRO,REMOTE (which clears the Kyber targets); W1 stays Maestro_Remote throughout."""
    s1 = link(bench, 1, "S1")
    l1, _ = _require_remote_pair(bench)
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    b1 = _slot(l1, 1, 1)[1]
    bad = []
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        if len(lines) > 8:
            raise Skip("W1 needs a free Maestro slot")
        with _wdp_off(w, before[1]):
            try:
                s1.listen()
                s1.probe.rule_clear()
                out = w.run(f"?KYBER,REMOTE,S1,M9:W1S1:{b1}", timeout=8)
                if _has(out, "Maestro 9: Local S1") or [t for t in snapshot(bench, 1) if t.upper().startswith("?MAESTRO,M9:")]:
                    bad.append("?KYBER stored Maestro id 9 as a slot")
                for cmd, want in ((";M95", "AA012705"), (";M9,goHome", "AA0122")):
                    m = s1.mark()
                    w.send(cmd)
                    problem = _exact(s1, m, bytes.fromhex(want))
                    if problem:
                        bad.append(f"{cmd} should reach Maestro 1 as target 9: {problem}")
                bench.note("clearing id 9: " + " | ".join(
                    "; ".join(w.run(c)) for c in ("?MAESTRO,CLEAR,M9", "?MAESTRO,CLEAR,M9:W1S1")))
            finally:
                bad += _rebuild(w, lines)
                w.run("?MAESTRO,REMOTE", timeout=6)
    assert not bad, "; ".join(bad)


@test("kyber.wdp_auto_remote_revert", "Taking W1 out of Maestro_Remote is reverted and persisted by WDP at the next advert while NaviCore is on the mesh (no reboot needed: the Maestro_Remote task never stopped)", needs=["wcb1"], links=[])
def wdp_auto_remote_revert(bench):
    """wdpEvaluateMaestroRemote runs on every accepted advert (WCB_WDP.cpp:636) and re-enables Maestro_Remote on a board
    with a local Maestro while a controller client is known (:468-485). ;W2,?WDP,POLL makes W2 advertise and solicits the
    mesh, so the revert need not wait for a periodic advert. Nothing is injected on S1 while it has two readers."""
    w = usb_wcb(bench)
    _require_remote_pair(bench)
    _require_kyber_broadcast_remote(w)
    with config_guard(bench, 1) as before:
        if "?WDP,OFF" in before[1]:
            raise Skip("W1's WDP is off")
        # A reboot earlier in the run (kyber.local_mode_s2 takes two) can leave W1 without NaviCore's row until the
        # next periodic advert, up to 60 s away. ?WDP,POLL solicits every board and client to advertise now.
        row, deadline = "", time.monotonic() + 20
        while "navicore" not in row.lower() and time.monotonic() < deadline:
            w.run("?WDP,POLL")
            time.sleep(3)
            row = next((x for x in w.run("?WDP,DUMP", timeout=8) if x.startswith("[WDP:N=20,")), "")
        if "navicore" not in row.lower():
            raise Skip("W1's WDP table has no NaviCore at 20, 20 s after soliciting adverts - is NaviCore on the mesh?")
        reverted = False
        try:
            wm = w.dev.mark()
            if not _has(w.run("?KYBER,CLEAR", timeout=6), "Kyber is Not used"):
                raise AssertionError("?KYBER,CLEAR did not print 'Kyber is Not used'")
            w.send(";W2,?WDP,POLL")
            pat = r"^\[WDP\] controller \(NaviCore/Sab\S\) on mesh \S enabling Maestro remote \(reboot to fully apply\)$"
            w.dev.expect(pat, timeout=70, since=wm)
            reverted = True
            w.dev.expect(r"^Kyber remote with broadcast mode$", timeout=3, since=wm)
            lst = _kyber_list(w)
            toks = snapshot(bench, 1)
        finally:
            if not reverted:
                w.run("?MAESTRO,REMOTE", timeout=6)
    assert "Kyber is Remote" in lst, f"?KYBER,LIST after the revert: {lst}"
    assert "?MAESTRO,REMOTE" in toks, "the revert was not persisted to the config chain"
