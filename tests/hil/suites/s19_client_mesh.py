"""The probe as a WCB_Client on the mesh: adoption, unicast, sendRaw, broadcasts, fragments, checksum and password
mismatches, re-join, and the mesh-mode specs deferred from the serial, variable and sequence areas.

Built from the verified client_mesh specs (plus the deferred mesh-mode ones). Rules from the specs:
- A WCB ACKs and executes a COMMAND from any in-group sender whose MAC matches its id, adopted or not
  (WCB.ino:4186-4201, 4254-4273); adoption only gates WCB -> client routing (;W<id>) and online tracking.
- ACK != executed on both sides (WCB.ino:4269-4312, WCB_Client.cpp:2744-2807): bytes are asserted on the wire.
- Each test that sends commands has its own probe id (MESH_IDS): a client restarts its seq numbers at every join,
  and a WCB clears a sender's duplicate ring only on a boot announce, which clients never send.
- Broadcast byte tests use ;S4/;S5: NaviCore writes mesh ;s1-;s3 to its own aux ports (NaviCore.ino:3066-3076).
- probe_in_mesh forgets the temporary peer on every WCB after leaving, so no 50 s stale-ACK window follows.
"""
import re
import time
import zlib
from contextlib import contextmanager

from hil.runner import Skip, test
from suites.common import (Console, Watch, config_guard, link, marker, mesh_params, nonce, padded, probe_in_mesh,
                           remote_wcbs, require_tokens, snapshot, token, usb_wcb)

# One id per test that sends commands (see the module docstring); s18's etm.rx_crc_gate_probe holds 14. Tests that
# only receive, only sendRaw (no sequence numbers), only send unensured JSON (never in the ring) or never reach a WCB
# (auth) may share. rejoin reboots W1 before it starts.
MESH_IDS = {"adopt": 18, "unicast": 17, "raw": 16, "broadcast": 13, "json": 12, "frag": 11, "whoami": 10,
            "checksum": 8, "auth": 7, "rejoin": 18, "leave": 15, "maestro_return": 16, "bcast_ports": 5,
            "tx_integrity": 15, "core0": 16, "raw_bounds": 16, "var_sets": 3, "seq_fanout": 4, "seq_body": 7}


def _has(lines, text):
    return any(text in x for x in lines)


def _dump(w):
    return [x.rstrip() for x in w.run("?WDP,DUMP", timeout=8)]


def _row(dump, n):
    return next((x for x in dump if x.startswith(f"[WDP:N={n},")), None)


def _wait_rx(probe, since, sender, predicate, timeout=2.0):
    """The first text the probe received from <sender> that satisfies predicate, or None."""
    deadline = time.monotonic() + timeout
    while True:
        hit = next((x for s, x in probe.mesh_received(since) if s == sender and predicate(x)), None)
        if hit is not None or time.monotonic() >= deadline:
            return hit
        time.sleep(0.1)


def _heard(probe, wcb, timeout=15.0):
    """Wait until the client lists <wcb> as online (it heard that board's heartbeat)."""
    deadline = time.monotonic() + timeout
    while wcb not in probe.mesh_state()[1]:
        if time.monotonic() >= deadline:
            raise AssertionError(f"the probe never heard WCB{wcb}'s heartbeat")
        time.sleep(1.0)


@contextmanager
def _client(bench, probe_name, key, adopted=False, **overrides):
    """The probe joined under MESH_IDS[key]. adopted=True waits for W1's adoption and ONLINE lines (up to ~25 s);
    otherwise a short settle is enough, because a WCB executes an in-group sender's commands before adoption."""
    w = usb_wcb(bench)
    cid = MESH_IDS[key]
    if _row(_dump(w), cid):
        raise Skip(f"mesh id {cid} is already in W1's WDP table")
    wm = w.dev.mark()
    with probe_in_mesh(bench, probe_name, cid, **overrides) as probe:
        if adopted:
            w.dev.expect(rf"\[WDP\] temporarily joined WCB{cid} ", timeout=12, since=wm)
            w.dev.expect(rf"\[ETM\] WCB{cid} came ONLINE", timeout=25, since=wm)
        else:
            time.sleep(2.0)
        yield probe, cid, wm


def _seq_of(lines, pattern):
    m = next((re.search(pattern, x) for x in lines if re.search(pattern, x)), None)
    return m.group(1) if m else None


# ============================================================ joining and unicast
@test("client_mesh.adopt_identity_route", "A temporary client is unreachable before it joins, adopted with the exact WDP row, then ;W<id> delivers and is ACKed (~30 s)", needs=["wcb1", "probe1"], links=[])
def adopt_identity_route(bench):
    w = usb_wcb(bench)
    cid = MESH_IDS["adopt"]
    bad = []
    dump0 = _dump(w)
    peers0 = int(re.search(r"PEERS=(\d+)", next((x for x in dump0 if x.startswith("[WDPCFG:")), "PEERS=0")).group(1))
    if "AUTOJOIN=0" in "".join(dump0):
        raise Skip("W1 auto-join is off")
    before = [x.rstrip() for x in w.run(f";W{cid},PINGHIL{nonce()}")]
    if f"WCB {cid} is not a reachable target — it isn't a configured or learned peer or the controller." not in before:
        bad.append(f";W{cid} before the join printed {before}")
    with _client(bench, "probe1", "adopt", adopted=True) as (probe, _, wm):
        lines = [x.rstrip() for x in w.dev.since(wm)]
        for want in (f"[WDP] learned WCB{cid} HILProbe", f"[PEER] WCB{cid} registered (live, TEMPORARY).",
                     f"[WDP] temporarily joined WCB{cid} HILProbe (temporary)"):
            if not _has(lines, want):
                bad.append(f"no {want!r}")
        if not any(re.search(rf"\[ETM\] WCB{cid} came ONLINE \(src MAC: 02:[0-9A-F]{{2}}:[0-9A-F]{{2}}:00:00:{cid:02X}\)", x) for x in lines):
            bad.append("the ONLINE line has the wrong MAC")
        dump = _dump(w)
        row = _row(dump, cid)
        if not re.match(rf"^\[WDP:N={cid},CLIENT=1,ALIAS=HILProbe,HW=0,HWREV=,FW=wcb_probe-2,CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=\d+,SEEN=1,PEER=4\]$", row or ""):
            bad.append(f"WDP row {row}")
        if any(x.startswith((f"[WDPIF:N={cid},", f"[WDPX:N={cid},", f"[WDPSEQ:N={cid},")) for x in dump):
            bad.append("a client has WDPIF/WDPX/WDPSEQ rows")
        if not _has(dump, f"PEERS={peers0 + 1}]"):
            bad.append("WDPCFG PEERS did not grow by one")
        base = w.etm_board_stats().get(cid)
        text = f"PINGHIL{nonce()}"
        pm = probe.dev.mark()
        w.send(f";W{cid},{text}")
        if _wait_rx(probe, pm, 1, lambda x: x == text, timeout=1.5) is None:
            bad.append(f"the probe did not receive ;W{cid}")
        time.sleep(1.5)
        after = w.etm_board_stats().get(cid)
        if not base or not after or (after["sent"], after["ackd"], after["failed"]) != (base["sent"] + 1, base["ackd"] + 1, base["failed"]):
            bad.append(f"WCB{cid} stats {base} -> {after}")
    assert not bad, "; ".join(bad)


@test("client_mesh.unicast_executes", "Client unicasts, ensured and best-effort, execute once on W1 and on W2, ACKed before execution", needs=["wcb1", "probe1"])
def unicast_executes(bench):
    s12, s23 = link(bench, 1, "S2"), link(bench, 2, "S3")
    w = usb_wcb(bench)
    n1, n2, n3 = marker("a"), marker("b"), marker("c")
    try:
        w.run("?DEBUG,ETM,ON")
        with _client(bench, "probe1", "unicast") as (probe, cid, _):
            watch, wm = Watch(s12, s23), w.dev.mark()
            probe.mesh_send(1, f";S2{n1}", ensured=True)
            probe.mesh_send(2, f";S3{n2}", ensured=True)
            watch.expect(s12, n1.encode() + b"\r", timeout=3)
            watch.expect(s23, n2.encode() + b"\r", timeout=3)
            probe.mesh_send(1, f";S2{n3}", ensured=False)
            watch.expect(s12, n3.encode() + b"\r", timeout=3)
            time.sleep(1.0)
            lines = [x.rstrip() for x in w.dev.since(wm)]
            got12, errs23 = watch.got(s12), s23.errors(watch.marks[s23.key])
    finally:
        w.run("?DEBUG,ETM,OFF")
    assert got12.count(n1.encode() + b"\r") == 1 and got12.count(n3.encode() + b"\r") == 1, f"W1 S2 got {got12!r}"
    assert not errs23, f"RXERR on W2's soft port: {errs23}"
    seq1 = _seq_of(lines, rf"\[ETM\] Received seq (\d+) from WCB{cid}: ;S2{n1}")
    assert seq1, "no Received line for the ensured unicast"
    ack = next((i for i, x in enumerate(lines) if f"[ETM] Sent ACK seq {seq1} to WCB{cid}, result: 0" in x), None)
    rec = next(i for i, x in enumerate(lines) if f"from WCB{cid}: ;S2{n1}" in x)
    assert ack is not None and ack < rec, "the ACK did not precede execution"
    seq3 = _seq_of(lines, rf"\[ETM\] Received seq (\d+) from WCB{cid}: ;S2{n3}")
    assert seq3 and _has(lines, f"[ETM] Sent ACK seq {seq3} to WCB{cid}, result: 0"), "the best-effort unicast was not ACKed (W1 ACKs every command)"


@test("client_mesh.sendraw_exact_bytes", "sendRaw writes exact bytes (no CR, no CRC) out a W1 hardware port and a W2 software port", needs=["wcb1", "probe1"])
def sendraw_exact_bytes(bench):
    s12, s23 = link(bench, 1, "S2"), link(bench, 2, "S3")
    w = usb_wcb(bench)
    try:
        assert _has(w.run("?DEBUG,RAW,ON"), "Raw serial debugging enabled")
        with _client(bench, "probe1", "raw") as (probe, cid, _):
            m12, m23, wm = s12.mark(), s23.mark(), w.dev.mark()
            ok1 = probe.mesh_raw(1, 2, bytes.fromhex("AA020400702E"))
            ok2 = probe.mesh_raw(2, 3, bytes.fromhex("DEADBEEF"))
            time.sleep(3)
            g12, g23, lines = s12.received(m12), s23.received(m23), [x.rstrip() for x in w.dev.since(wm)]
    finally:
        w.run("?DEBUG,RAW,OFF")
    assert ok1 and ok2, "the probe could not queue the raw frames"
    assert g12 == bytes.fromhex("AA020400702E"), f"W1 S2 got {g12.hex(' ')}"
    assert g23 == bytes.fromhex("DEADBEEF"), f"W2 S3 got {g23.hex(' ')}"
    assert _has(lines, f"[RAW] W{cid} → S2 (ESP-NOW)  6 bytes: AA 02 04 00 70 2E"), "no [RAW] debug line"


@test("client_mesh.sendraw_real_maestro_tap", "sendRaw to the real Maestro line on W2 S1 arrives byte-exact on the tap (moves Maestro 2 channel 0 to 1500 µs)", needs=["wcb1", "probe1"])
def sendraw_real_maestro_tap(bench):
    tap = link(bench, 2, "S1")
    frame = bytes.fromhex("AA020400702E")        # Pololu setTarget, device 2, channel 0, 6000 quarter-us
    with _client(bench, "probe1", "raw") as (probe, _, _):
        tm = tap.mark()
        ok = probe.mesh_raw(2, 1, frame)
        tap.expect(frame, timeout=1.5, since=tm)
        time.sleep(0.5)
        got = tap.received(tm)
    assert ok and got == frame, f"the Maestro line carried {got.hex(' ')}"


# ============================================================ broadcasts
@test("client_mesh.broadcast_reaches_all_ports", "A client's ensured ;S4 broadcast runs on every WCB once, is ACKed, and is not re-broadcast", needs=["wcb1", "probe1"])
def broadcast_reaches_all_ports(bench):
    s14, s24 = link(bench, 1, "S4"), link(bench, 2, "S4")
    w = usb_wcb(bench)
    t = marker()
    try:
        w.run("?DEBUG,ETM,ON")
        with _client(bench, "probe1", "broadcast") as (probe, cid, _):
            watch, wm = Watch(s14, s24), w.dev.mark()
            ok = probe.mesh_broadcast(f";S4{t}", ensured=True)
            watch.expect(s14, t.encode() + b"\r", timeout=3)
            watch.expect(s24, t.encode() + b"\r", timeout=3)
            time.sleep(1.0)
            g14, g24, lines = watch.got(s14), watch.got(s24), [x.rstrip() for x in w.dev.since(wm)]
    finally:
        w.run("?DEBUG,ETM,OFF")
    assert ok, "the probe refused the broadcast"
    assert g14.count(t.encode() + b"\r") == 1 and g24.count(t.encode() + b"\r") == 1, f"W1 S4 {g14!r} / W2 S4 {g24!r}"
    assert any(re.search(rf"\[ETM\] Sent ACK seq \d+ to WCB{cid}, result: 0", x) for x in lines), "a non-JSON client broadcast was not ACKed"
    assert not any(re.search(rf"\[ETM\] Sent seq \d+: ;S4{t}", x) for x in lines), "W1 re-broadcast a mesh-received command"


@test("client_mesh.json_broadcast_once", "(should) A client's ensured JSON broadcast is processed once per WCB, not once plus an ACKed unicast retry", needs=["wcb1", "probe1"], links=[])
def json_broadcast_once(bench):
    """The WCB and WCB_Client disagree about who tracks a JSON broadcast: the WCB never ACKs a target-0 '{' frame
    (WCB.ino:4257-4268), but WCB_Client::broadcast() defaults to ensured=true and waits for every online board
    (WCB_Client.h:621, WCB_Client.cpp:2208-2218). So each WCB gets the JSON twice — the broadcast, then the client's
    ACKed unicast retry. NaviCore's WCB_SEND target 0 uses that default (NaviCore.ino:4035-4036)."""
    w = usb_wcb(bench)
    text = '{"t":"HIL' + nonce() + '"}'
    try:
        w.run("?DEBUG,ETM,ON")
        with _client(bench, "probe1", "json") as (probe, cid, _):
            _heard(probe, 1)                      # only boards the client has heard are expected to ACK
            wm = w.dev.mark()
            probe.mesh_broadcast(text, ensured=True)
            time.sleep(3)
            lines = [x.rstrip() for x in w.dev.since(wm)]
    finally:
        w.run("?DEBUG,ETM,OFF")
    received = [x for x in lines if re.search(rf"\[ETM\] Received seq \d+ from WCB{cid}: " + re.escape(text), x)]
    bench.note(f"client JSON broadcast processed {len(received)} time(s) on W1")
    assert len(received) == 1, f"W1 processed the JSON broadcast {len(received)} times"


# ============================================================ limits and round trips
def _crc(text):
    return "%08X" % (zlib.crc32(text.encode()) & 0xFFFFFFFF)


@test("client_mesh.fragmented_unicast_limits", "Client unicasts: 187 characters go as one packet; 188 and 400 are fragmented and run once; a 188-character broadcast is refused", needs=["wcb1", "probe1"])
def fragmented_unicast_limits(bench):
    """The client's single-packet limit with checksums on is 187 (WCB_Client.cpp:404-410), matching the WCB's
    ETM_MAX_CMD_WITH_CRC (WCB.ino:225). Longer unicasts go as MGMT fragments, 3 passes each, one job at a time
    (WCB_Client.cpp:442-540); a broadcast is never fragmented (WCB_Client.cpp:386-393)."""
    s12, s24 = link(bench, 1, "S2"), link(bench, 2, "S4")
    w = usb_wcb(bench)

    def payload(port, tag, n):
        return f";S{port}" + padded(tag, n - 3)      # [0-9A-Z] only: no '^', '|', '*' or whitespace

    bad = []
    try:
        assert _has(w.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled")
        with _client(bench, "probe1", "frag") as (probe, cid, _):
            for p in (payload(2, "a", 187), payload(2, "b", 188), payload(2, "c", 400)):
                body = p[3:].encode() + b"\r"
                m, pm, wm = s12.mark(), probe.dev.mark(), w.dev.mark()
                probe.mesh_send(1, p, ensured=True)
                try:
                    s12.expect(body, timeout=4, since=m)
                except AssertionError:
                    bad.append(f"{len(p)} chars were not delivered")
                time.sleep(1.2)                        # one fragment job at a time
                if s12.received(m).count(body) != 1:
                    bad.append(f"{len(p)} chars ran {s12.received(m).count(body)} times")
                if (len(p) > 187) != _has(probe.dev.since(pm), "fragmenting"):
                    bad.append(f"{len(p)} chars: fragmented is {_has(probe.dev.since(pm), 'fragmenting')}")
                if len(p) == 188:
                    wl = w.dev.since(wm)
                    if not any(re.search(r"\[MGMT\] New session [0-9A-Fa-f]{4} — expecting 2 chunk\(s\) \(client unicast\)", x) for x in wl) \
                            or not any(re.search(r"\[MGMT\] Session [0-9A-Fa-f]{4} complete — executing 188 chars", x) for x in wl):
                        bad.append("W1 did not log the 2-chunk session and its execution")
            m, pm = s24.mark(), probe.dev.mark()
            ok = probe.mesh_broadcast(payload(4, "d", 188), ensured=True)
            time.sleep(3)
            if ok or not _has(probe.dev.since(pm), "broadcast: command too long (188 > 187 chars)") or s24.received(m):
                bad.append(f"the 188-character broadcast: queued {ok}, W2 S4 got {s24.received(m)!r}")
    finally:
        w.run("?DEBUG,MGMT,OFF")
    assert not bad, "; ".join(bad)


@test("client_mesh.whoami_roundtrip", "?WHOAMI from the client gets each WCB's alias JSON back, and nothing reaches a port", needs=["wcb1", "probe1"], links=[])
def whoami_roundtrip(bench):
    w = usb_wcb(bench)
    aliases = {n: token(snapshot(bench, n), "?ALIAS,") for n in (1, 2)}
    if not all(aliases.values()):
        raise Skip("W1 or W2 has no alias")
    aliases = {n: a[len("?ALIAS,"):] for n, a in aliases.items()}
    got = {}
    with _client(bench, "probe1", "whoami") as (probe, _, wm):
        for n, cmd in ((1, "?WHOAMI"), (2, "?whoami")):          # case-insensitive
            for _ in range(2):                                  # the reply is untracked: re-ask once on a radio loss
                pm = probe.dev.mark()
                probe.mesh_send(n, cmd)
                got[n] = _wait_rx(probe, pm, n, lambda x: x.startswith('{"type":"wcb_alias"'), timeout=1.5)
                if got[n]:
                    break
        lines = w.dev.since(wm)
    for n in (1, 2):
        assert got[n] == f'{{"type":"wcb_alias","id":{n},"alias":"{aliases[n]}"}}', f"W{n} answered {got[n]!r}"
    assert not _has(lines, "Invalid Serial Command"), "?WHOAMI reached W1's command parser"


@test("client_mesh.wcb_broadcast_reaches_client", "A WCB's ETM broadcast (a top-level ;C recall of a missing key) reaches the client exactly once", needs=["wcb1", "probe1"], links=[])
def wcb_broadcast_reaches_client(bench):
    w = usb_wcb(bench)
    key = f"HIL{nonce()}"                    # a key that does not exist: the lookup is a read-only NVS open
    with _client(bench, "probe1", "adopt") as (probe, _, _):       # receive-only, so it may share the adopt id
        pm, wm = probe.dev.mark(), w.dev.mark()
        w.send(f";C{key}")
        time.sleep(3)
        hits = [x for s, x in probe.mesh_received(pm) if s == 1 and x == f";C{key}"]
        lines = w.dev.since(wm)
    assert len(hits) == 1, f"the client received the broadcast {len(hits)} times"
    assert _has(lines, f"Recall stored command request received:C{key}") and _has(lines, f"No command stored under key: '{key}'"), "W1 recall lines"


# ============================================================ mismatches and lifecycle (probe2 joins; probe1 watches W1)
@test("client_mesh.checksum_mismatch_both_ways", "Characterization: a checksum-off client is adopted, online and ACKed, yet its commands are discarded; WCB commands reach it with the |CRC tail", needs=["wcb1", "probe2"])
def checksum_mismatch_both_ways(bench):
    """Design trap: such a client looks healthy — WDP-adopted, ETM-online, every command ACKed (WCB.ino:4269-4273) —
    while everything it sends is dropped at the CRC check (WCB.ino:4302-4312); the only evidence is the
    ?DEBUG,ETM-gated 'rejected: missing CRC'. CLAUDE.md rule 4's 'a mismatch rejects all packets' holds for commands
    only: adoption, heartbeats and ACKs keep working."""
    s12 = link(bench, 1, "S2")
    require_tokens(bench, 1, "?ETM,CHKSM,ON")
    w = usb_wcb(bench)
    n1, n2 = marker("a"), f"PINGHIL{nonce()}"
    try:
        w.run("?DEBUG,ETM,ON")
        with _client(bench, "probe2", "checksum", adopted=True, checksum=False) as (probe, cid, _):
            m, wm = s12.mark(), w.dev.mark()
            probe.mesh_send(1, f";S2{n1}", ensured=True)
            time.sleep(3)
            lines, got = [x.rstrip() for x in w.dev.since(wm)], s12.received(m)
            base = w.etm_board_stats().get(cid)
            pm = probe.dev.mark()
            w.send(f";W{cid},{n2}")
            rx = _wait_rx(probe, pm, 1, lambda x: x.startswith(n2), timeout=2.0)
            time.sleep(1.0)
            after = w.etm_board_stats().get(cid)
    finally:
        w.run("?DEBUG,ETM,OFF")
    seq = _seq_of(lines, r"\[ETM\] seq (\d+) rejected: missing CRC")
    assert n1.encode() not in got, "W1 executed a CRC-less command"
    assert seq and _has(lines, f"[ETM] Sent ACK seq {seq} to WCB{cid}, result: 0"), "the discarded command was not ACKed first"
    assert rx == f"{n2}|CRC{_crc(n2)}", f"the checksum-off client received {rx!r}"
    assert base and after and after["ackd"] >= base["ackd"] + 1, f"W1 did not count the client's ACK: {base} -> {after}"


@test("client_mesh.auth_mismatch_silent", "A client with the wrong mesh password or MAC octet is deaf and mute, and no WCB state is created (~50 s)", needs=["wcb1", "probe2"])
def auth_mismatch_silent(bench):
    """Both gates drop the packet before any debug print: the octet check (WCB.ino:4034) and the ETM password check
    (WCB.ino:4161); the client drops the WCBs' heartbeats the same way (WCB_Client.cpp:2631-2634, 2660-2662). The
    passwords are never printed."""
    s12 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    cid = MESH_IDS["auth"]
    params = mesh_params(bench)
    wrong = params["password"][:-1] + ("X" if params["password"][-1:] != "X" else "Y")
    bad = []
    try:
        w.run("?DEBUG,ETM,ON")
        for label, override in (("password", dict(password=wrong)), ("MAC octet", dict(oct3=(params["oct3"] + 1) & 0xFF))):
            wm = w.dev.mark()
            with probe_in_mesh(bench, "probe2", cid, forget=False, **override) as probe:
                time.sleep(15)
                online = probe.mesh_state()[1]
                m, t = s12.mark(), marker("x")
                queued = probe.mesh_send(1, f";S2{t}")
                time.sleep(3)
                pm = probe.dev.mark()
                out = w.run(f";W{cid},PINGHIL{nonce()}")
                time.sleep(1.0)
                if online:
                    bad.append(f"{label}: the client heard {online}")
                if not queued:
                    bad.append(f"{label}: MSEND was not queued")
                if s12.received(m):
                    bad.append(f"{label}: W1 executed the command")
                if _has(w.dev.since(wm), f"WCB{cid}"):
                    bad.append(f"{label}: W1 logged WCB{cid}")
                if _row(_dump(w), cid):
                    bad.append(f"{label}: W1 learned the client")
                if not _has(out, f"WCB {cid} is not a reachable target"):
                    bad.append(f"{label}: ;W{cid} printed {out}")
                if probe.mesh_received(pm):
                    bad.append(f"{label}: the client received W1's command")
    finally:
        w.run("?DEBUG,ETM,OFF")
    assert not bad, "; ".join(bad)


@test("client_mesh.rejoin_seq_reuse", "(should) A client that re-joins under the same id has its first commands executed, not ACKed and dropped as duplicates (2 W1 reboots)", needs=["wcb1", "probe2"])
def rejoin_seq_reuse(bench):
    """Probable bug: WCB_Client never sends an ETM boot announce (WCB_Client.h:49-52, 221-224) and restarts its seq
    counter at every begin() (WCB_Client.cpp:77, 2183), while a WCB clears a sender's 8-entry duplicate ring only on a
    boot announce (WCB.ino:235, 4219-4228) — never on eviction or ?WDP,FORGET. After any client reboot (NaviCore, a
    MgmtRelay, this probe) its first commands are ACKed and silently not executed. W1 is rebooted before (a clean
    ring) and after (so later sessions with this id start clean)."""
    s12 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    cid = MESH_IDS["rejoin"]
    counts = {}
    w.reboot()
    try:
        for session, n in (("A", 3), ("B", 4)):
            ms = [marker(f"{session}{k}") for k in range(n)]
            m = s12.mark()
            with probe_in_mesh(bench, "probe2", cid) as probe:
                time.sleep(2)
                for x in ms:
                    probe.mesh_send(1, f";S2{x}", ensured=True)
                    time.sleep(0.6)
                time.sleep(2)
            got = s12.received(m)
            counts[session] = [got.count(x.encode() + b"\r") for x in ms]
    finally:
        w.reboot()
    bench.note(f"executions per command: {counts}")
    assert counts["A"] == [1, 1, 1], f"session A (control): {counts['A']}"
    assert counts["B"] == [1, 1, 1, 1], f"after re-joining, session B: {counts['B']}"


@test("client_mesh.leave_stale_temp_peer", "The probe refuses a second join; leaving reboots it; the stale temporary peer fails a broadcast's ACK until W1 evicts it at 50 s (slow, ~75 s)", needs=["wcb1", "probe2"], links=[])
def leave_stale_temp_peer(bench):
    w = usb_wcb(bench)
    cid = MESH_IDS["leave"]
    params = mesh_params(bench)
    k1, k2 = f"HIL{nonce()}", f"HIL{nonce()}"
    wm = w.dev.mark()
    with probe_in_mesh(bench, "probe2", cid, forget=False) as probe:
        w.dev.expect(rf"\[ETM\] WCB{cid} came ONLINE", timeout=25, since=wm)
        try:
            probe.mesh_join(cid, params["oct2"], params["oct3"], params["password"], params["quantity"],
                            channel=params["channel"], checksum=params["checksum"])
            second = "accepted"
        except AssertionError as e:
            second = str(e)
        t0, lm, pm = time.monotonic(), w.dev.mark(), probe.dev.mark()
        probe.mesh_leave()
        booted = probe.dev.since(pm)
    sm = w.dev.mark()
    w.send(f";C{k1}")                         # a top-level recall broadcasts; the stale peer is in its ACK set
    time.sleep(4)
    stale = [x for x in w.dev.since(sm) if re.search(rf"\[ETM\] WCB{cid} failed to ACK seq \d+ after 3 retries: ;C{k1}", x)]
    try:
        w.dev.expect(rf"\[PEER\] temporary WCB{cid} evicted — silent for \d+s", timeout=max(1.0, t0 + 60 - time.monotonic()), since=lm)
        evicted_after = round(next(ts for ts, x in list(w.dev.lines[lm:]) if f"temporary WCB{cid} evicted" in x) - t0, 1)
    except AssertionError:
        evicted_after = None
    after = w.dev.since(lm)
    em = w.dev.mark()
    w.send(f";C{k2}")
    time.sleep(3)
    for n in remote_wcbs(bench):              # W2 evicts on the same TTL; don't leave it to the next test
        w.send(f";W{n},?WDP,FORGET,{cid}")
    bench.note(f"stale temporary peer evicted {evicted_after} s after MESH LEAVE")
    assert "already joined" in second, f"a second MESH JOIN was {second}"
    assert _has(booted, "OK MESH LEAVE rebooting") and any("BOOT wcb_probe" in x for x in booted), "leave did not reboot the probe"
    assert stale, "the broadcast did not wait on the stale temporary peer"
    assert evicted_after is not None and 38 <= evicted_after <= 55, f"evicted {evicted_after} s after leaving"
    assert not _has(after, f"[ETM] WCB{cid} went OFFLINE"), "the temporary peer went OFFLINE before its eviction"
    assert not _row(_dump(w), cid), "the evicted peer still has a WDP row"
    assert not _has(w.dev.since(em), f"WCB{cid} failed to ACK"), "a broadcast after eviction still waited on the peer"


# ============================================================ mesh-mode specs from the serial areas
@test("input.w2_maestro_return_path", "A real Maestro reply on W2 S1 outside a query is bridged to W1 S1 when W2 is Maestro_Remote, and held as text otherwise", needs=["wcb1", "probe2"])
def w2_maestro_return_path(bench):
    """Writes the real Maestro 2 line: getErrors only, which clears its error flags. W2 answers from its WiFi task
    (WCB.ino:4586-4596); a Maestro_Remote W2's KyberRemoteTask forwards the reply as raw target 98 (WCB.ino:2700-2745)."""
    w1s1, tap = link(bench, 1, "S1"), link(bench, 2, "S1")
    remote = "?MAESTRO,REMOTE" in [t.upper() for t in snapshot(bench, 2)]
    with _client(bench, "probe2", "maestro_return") as (probe, _, _):
        tm, m1 = tap.mark(), w1s1.mark()
        queued = probe.mesh_raw(2, 1, bytes.fromhex("AA0221"))
        tap.expect(bytes.fromhex("AA0221"), timeout=1.0, since=tm)
        time.sleep(2.0)
        bridged = w1s1.received(m1)
    bench.note(f"W2 Maestro_Remote {remote}; W1 S1 received {bridged.hex(' ')}")
    assert queued, "the probe could not queue the raw frame"
    if remote:
        assert len(bridged) == 2, f"the Maestro's 2-byte reply was not bridged to W1 S1: {bridged.hex(' ')}"
    else:
        assert not bridged, f"W1 S1 got {bridged.hex(' ')} although W2 is not Maestro_Remote"


@test("input.mesh_bcast_to_ports", "A mesh client's plain broadcast reaches every eligible port on both WCBs, skips the Maestro port, and is not re-broadcast", needs=["wcb1", "probe2"])
def mesh_bcast_to_ports(bench):
    w1s1, w1s2, w1s3, w2s3 = link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 2, "S3")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,OUT,S3,ON")
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON")
    w = usb_wcb(bench)
    t = marker()
    try:
        w.run("?DEBUG,ETM,ON")
        with _client(bench, "probe2", "bcast_ports") as (probe, cid, _):
            watch, wm = Watch(w1s1, w1s2, w1s3, w2s3), w.dev.mark()
            probe.mesh_broadcast(t, ensured=True)
            for l in (w1s2, w1s3, w2s3):
                watch.expect(l, t.encode() + b"\r", timeout=3)
            time.sleep(1.5)
            maestro_port, lines = watch.got(w1s1), [x.rstrip() for x in w.dev.since(wm)]
    finally:
        w.run("?DEBUG,ETM,OFF")
    assert t.encode() not in maestro_port, "the broadcast reached W1's Maestro port"
    assert any(re.search(rf"\[ETM\] Sent ACK seq \d+ to WCB{cid}, result: 0", x) for x in lines), "W1 did not ACK the client's broadcast"
    assert not any(re.search(rf"\[ETM\] Sent seq \d+: {t}", x) for x in lines), "W1 re-broadcast a mesh-received line"


def _json_load(probe, seconds):
    """Unensured JSON broadcasts at ~50 Hz: consumed silently by every WCB and never in a duplicate ring."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        probe.mesh_broadcast('{"hil":1}', ensured=False)
        time.sleep(0.02)


def _chain_run(w, l, port, tag, probe=None):
    """20 USB lines of 10 ';S<port>' commands (~60 chars each), with or without mesh load -> (exact, wrong, RXERR)."""
    texts = [[padded(f"{tag}{i:02d}{k}", 61) for k in range(10)] for i in range(20)]
    m = l.mark()
    for row in texts:
        w.send("^".join(f";S{port}{x}" for x in row))
        if probe:
            _json_load(probe, 0.8)
        else:
            time.sleep(0.8)
    time.sleep(2.0)
    got = l.received(m)
    exact = sum(1 for row in texts for x in row if got.count(x.encode() + b"\r") == 1)
    return exact, 200 - exact, len(l.errors(m))


@test("input.softserial_tx_integrity", "Rule 13: a protected soft port's TX stays byte-exact with and without mesh load; an unprotected port is measured for comparison (slow, ~2 min)", needs=["wcb1", "probe2"])
def softserial_tx_integrity(bench):
    """applySoftSerialIntTx protects S3-S5 only where no core-0 task writes the port (WCB.ino:2016-2068); a raw mapping
    makes S5 unprotected. What disturbs an unprotected byte is not established (tick, time-slicing with other
    priority-1 core-1 tasks), so the unprotected arm is recorded, not asserted."""
    s3, s5 = link(bench, 1, "S3"), link(bench, 1, "S5")
    require_tokens(bench, 1, "?BAUD,S3,9600", "?BAUD,S5,9600", "?BCAST,OUT,S5,ON", "?BCAST,IN,S5,ON")
    w = usb_wcb(bench)
    results = {}
    with _client(bench, "probe2", "tx_integrity") as (probe, _, _):
        results["protected, mesh load"] = _chain_run(w, s3, 3, "A", probe)
        results["protected, no load"] = _chain_run(w, s3, 3, "B")
        with config_guard(bench, 1):
            try:
                w.run("?DEBUG,ON")
                w.run("?MAP,SERIAL,S5,R,S4")
                unprotected = _has(w.run("?BAUD,S5,9600"), "[SOFTSERIAL] S5 bit-bang TX: unprotected (a core-0 task can write this port)")
                w.run("?DEBUG,OFF")
                results["unprotected, mesh load"] = _chain_run(w, s5, 5, "C", probe)
            finally:
                w.run("?DEBUG,OFF")
                w.run("?MAP,SERIAL,CLEAR,S5")
                w.run("?BAUD,S5,9600")
    bench.note("soft-serial TX (exact, wrong, RXERR): " + "; ".join(f"{k}: {v}" for k, v in results.items()))
    assert unprotected, "the raw mapping did not report S5 as unprotected"
    for arm in ("protected, mesh load", "protected, no load"):
        assert results[arm][1:] == (0, 0), f"{arm}: {results[arm]}"


@test("input.softserial_core0_contention", "A core-0 raw write to protected S3 while the loop task writes S4 (shared soft-serial mutex): both stay exact, W1 stays up", needs=["wcb1", "probe2"])
def softserial_core0_contention(bench):
    """Each 151-byte MRAW bit-bangs ~157 ms on W1's WiFi task (WCB.ino:4586-4596, SoftwareSerial.h:364 static mux), so
    ETM retries during the run are expected; a watchdog reboot of W1 fails the test (the residual WCB.ino:2031-2041
    documents, CLAUDE.md rule 11)."""
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    blocks = [padded(f"R{i}", 150).encode() + b"\r" for i in range(10)]
    lines = [padded(f"L{i:02d}", 64) for i in range(30)]
    with _client(bench, "probe2", "core0") as (probe, _, _):
        m3, m4, wm = s3.mark(), s4.mark(), w.dev.mark()
        for i in range(10):
            probe.mesh_raw(1, 3, blocks[i])
            for j in range(3):
                w.send(f";S4,{lines[i * 3 + j]}")
            time.sleep(0.3)
        time.sleep(3)
        g3, g4, e3, e4 = s3.received(m3), s4.received(m4), s3.errors(m3), s4.errors(m4)
        rebooted, offline = w.rebooted_since(wm), _has(w.dev.since(wm), "[ETM] WCB2 went OFFLINE")
    exact3 = sum(g3.count(b) == 1 for b in blocks)
    exact4 = sum(g4.count(x.encode() + b"\r") == 1 for x in lines)
    assert not rebooted, "W1 rebooted during the contention run"
    assert not offline, "W1 marked WCB2 offline during the run"
    assert exact3 == 10 and not e3, f"S3 raw blocks: {exact3}/10 exact, RXERR {len(e3)}"
    assert exact4 == 30 and not e4, f"S4 lines: {exact4}/30 exact, RXERR {len(e4)}"


@test("map.probe_mesh_raw_chunk_bounds", "A 177-byte sendRaw arrives byte-exact on W2 S2; the probe refuses larger or out-of-range frames; a wrong-password client is ignored", needs=["wcb1", "probe1"])
def probe_mesh_raw_chunk_bounds(bench):
    s22 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    pattern = bytes(range(177))                   # RAW_SERIAL_MAX_CHUNK (WCB.ino:230)
    params = mesh_params(bench)
    wrong = params["password"][:-1] + ("X" if params["password"][-1:] != "X" else "Y")
    with config_guard(bench, 1, 2):
        with _client(bench, "probe1", "raw_bounds") as (probe, _, _):
            m = s22.mark()
            queued = probe.mesh_raw(2, 2, pattern)
            try:
                s22.expect(pattern, timeout=1.5, since=m)
            except AssertionError:
                pass
            got, errs = s22.received(m), s22.errors(m)
            try:
                probe.mesh_raw(2, 2, bytes(178))
                oversize = "accepted"
            except AssertionError as e:
                oversize = str(e)
            port0, port6 = probe.mesh_raw(2, 0, b"x"), probe.mesh_raw(2, 6, b"x")
        with probe_in_mesh(bench, "probe1", MESH_IDS["raw_bounds"], forget=False, password=wrong) as probe:
            time.sleep(2)
            m = s22.mark()
            probe.mesh_raw(2, 2, bytes(range(16)))
            time.sleep(2)
            ignored = not s22.received(m)
    assert queued and got == pattern and not errs, f"W2 S2 got {len(got)} bytes, RXERR {len(errs)}"
    assert "177 bytes max" in oversize, f"a 178-byte MRAW was {oversize}"
    assert port0 is False and port6 is False, f"ports 0/6 queued: {port0}, {port6}"
    assert ignored, "a wrong-password client's raw frame reached W2 S2"


# ============================================================ mesh-mode specs from variables and sequences
def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _vget(w, name):
    return next((x.rstrip() for x in w.run(f"?VAR,GET,{name}") if x.startswith("[VAR]")), None)


@test("var.mesh_client_sets", "Characterization: a mesh client's broadcast ;V sets every WCB; its ;M! is limited to m1..m8 names, but its ;VP is not", needs=["wcb1", "probe1"], links=[])
def mesh_client_sets(bench):
    """Comment/code gap: WCB_Maestro.cpp:500-501 says the m1-m8 check exists so 'a mesh peer must not be able to set
    arbitrary WCB variables', yet any in-group peer's ;V and ;VP — even ?VAR,CLEAR,ALL — run (WCB.ino:4453-4464); the
    guard only limits ;M!. This is also the one way a broadcast ;V happens (docs/VARIABLES_DESIGN.md:129)."""
    w = usb_wcb(bench)
    with config_guard(bench, 1), Console(bench, 2) as c2:
        for n in ("hilbv", "hilx", "m7hil", "hilpp"):
            w.run(f"?VAR,CLEAR,{n}")
        _crun(c2, "?VAR,CLEAR,hilbv")
        try:
            with _client(bench, "probe1", "var_sets") as (probe, _, _):
                probe.mesh_broadcast(";V,hilbv,1")
                time.sleep(2)
                on_w1 = _vget(w, "hilbv")
                on_w2 = next((x for x in _crun(c2, "?VAR,GET,hilbv") if x.startswith("[VAR]")), None)
                probe.mesh_send(1, ";M!hilx=5")
                probe.mesh_send(1, ";M!m7hil=5")
                time.sleep(1)
                hilx, m7hil = _vget(w, "hilx"), _vget(w, "m7hil")
                probe.mesh_send(1, ";VP,hilpp,3")
                time.sleep(1)
                listing = [x.rstrip() for x in w.run("?VAR,LIST")]
        finally:
            for n in ("hilbv", "m7hil", "hilpp"):
                w.run(f"?VAR,CLEAR,{n}")
            _crun(c2, "?VAR,CLEAR,hilbv")
    assert on_w1 == "[VAR] hilbv = 1" and on_w2 == "[VAR] hilbv = 1", f"broadcast ;V: W1 {on_w1!r}, W2 {on_w2!r}"
    assert hilx == "[VAR] 'hilx' is not set (reads as 0)", f";M!hilx from a client: {hilx!r}"
    assert m7hil == "[VAR] m7hil = 5", f";M!m7hil from a client: {m7hil!r}"
    assert "  hilpp = 3  [persistent]" in listing, "a client's ;VP did not create the persistent variable"


@test("seq.fanout_wire_probe", "Seen from a client: a top-level ;C / ;SEQ fans out one normalized broadcast; ,L, nested and mesh-received recalls do not", needs=["wcb1", "probe1"], links=[])
def fanout_wire_probe(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        w.run("?SEQ,CLEAR,HILFO")
        try:
            w.run("?SEQ,SAVE,HILFO,;CHILFW")          # W1 has no HILFW, so every recall of it reports missing
            with _client(bench, "probe1", "seq_fanout") as (probe, _, _):
                def window(*actions):
                    pm, wm = probe.dev.mark(), w.dev.mark()
                    for act in actions:
                        act()
                    time.sleep(2)
                    return [x for s, x in probe.mesh_received(pm) if s == 1], [x.rstrip() for x in w.dev.since(wm)]

                r1, l1 = window(lambda: w.send(";CHILFW,L"), lambda: w.send(";CHILFW,LOCAL"))
                r2, _ = window(lambda: w.send(";cHILFW"))
                r3, _ = window(lambda: w.send(";seqHILFW"))
                r4, _ = window(lambda: w.send(";CHILFO"))
                r5, l5 = window(lambda: probe.mesh_send(1, ";CHILFO"))
        finally:
            w.run("?SEQ,CLEAR,HILFO")
    assert not any("HILFW" in x for x in r1) and _has(l1, "No command stored under key: 'HILFW'"), f",L windows: client got {r1}"
    assert r2.count(";CHILFW") == 1, f";cHILFW: client got {r2} (the prefix is rebuilt as ';C')"
    assert r3.count(";SEQHILFW") == 1, f";seqHILFW: client got {r3}"
    assert r4.count(";CHILFO") == 1 and not any("HILFW" in x for x in r4), f";CHILFO: client got {r4} (the nested recall stays local)"
    assert ";CHILFO" not in r5 and _has(l5, "Recalling command for key 'HILFO': ;CHILFW"), f"mesh-received recall: client got {r5}"


@test("seq.peer_body_broadcasts", "A mesh-triggered sequence whose body is plain text still broadcasts (origin forced local), without re-fanning the trigger", needs=["wcb1", "probe1"])
def peer_body_broadcasts(bench):
    s12 = link(bench, 1, "S2")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON")
    w = usb_wcb(bench)
    t = f"hilpb{nonce().lower()}"                  # inert: it reaches every broadcast port and NaviCore
    with config_guard(bench, 1):
        try:
            w.run(f"?SEQ,SAVE,HILPB,{t}")
            with _client(bench, "probe1", "seq_body") as (probe, _, _):
                m, pm = s12.mark(), probe.dev.mark()
                probe.mesh_send(1, ";CHILPB")
                try:
                    s12.expect(t.encode() + b"\r", timeout=3, since=m)
                except AssertionError:
                    pass
                time.sleep(2)
                got, rx = s12.received(m), [x for s, x in probe.mesh_received(pm) if s == 1]
        finally:
            w.run("?SEQ,CLEAR,HILPB")
    assert t.encode() + b"\r" in got, "the recalled plain-text body did not reach W1 S2"
    assert t in rx, f"the body was not broadcast to the mesh: client got {rx}"
    assert ";CHILPB" not in rx, "the mesh-received trigger was re-broadcast"
