"""NaviCore beyond the basics (Maestro over the mesh, serial routing, WCB_SEND, bridged JSON, TEST_ACTION, TRIGGER,
record/replay observability, #L diagnostics, a temporary probe) and the SBUS controller's exact channel values.

Built from the verified navicore_sbus specs. Rules from the specs:
- Never change NaviCore's Maestro slots or device ids, FORGET a learned peer, record, or save config: all write
  NaviCore flash. Never #L2 (restart) or #L20/#L21 (HCR frames). The SBUS controller's serial lines always start with
  '{' ('m' and 'w' outside JSON save to flash); its mode/cfg/wificfg commands also save and are never sent (SbusCtl has
  no method for them).
- GET_CONFIG prints the mesh password, and getcfg the controller's WiFi credentials: session.log hashes both as they
  arrive (Bench.log's filter on NaviCore and SBUS lines; SbusCtl.cfg hashes getcfg's line too).
- NaviCore can hold a finished line unsent until more output follows it, so a ?MAE query is followed by a harmless #L12
  that releases its [MAE:n] marker (NaviCore.cli; the marker itself ends in a newline, NaviCore.ino:741-756).
- Any ;W20,{json} from W1 opens a 20 s relay window: W1 USB then carries '"sys":1' lines; checks match by substring.
- SBUS channels are moved only when NaviCore's config binds nothing to them (hil.sbus.safe_channels); a bound channel can
  fire real actions. The one exception is the matrix channel, pressed only onto a slot with no mapping in the active mode
  (hil.sbus.matrix_button, sbus.trim_exact), which emits rc_trig and runs nothing.
The NaviCore and controller helpers live in hil/navicore.py and hil/sbus.py (docs/hil_plan/NAVICORE.md INF1, INF2).
"""
import re
import time

from hil.navicore import DBG_MAESTRO, DBG_SERIAL, DBG_WCB, NaviCore, SBUS_FULL_FPS
from hil.runner import Skip, test
from hil.sbus import SBUS_CENTER, SBUS_MAX, SbusCtl, band, matrix_button, safe_channels
from hil.wcb import PULL_MAX, WCB, Pull, PullRefused, pull_config
from suites.common import (Console, Watch, config_guard, link, marker, nonce, padded, probe_in_mesh, remote_wcbs,
                           require_tokens, snapshot, usb_wcb)
from suites.s03_wcb import _clear, _factory_reply, _grow_over, _pull_lines, _reply_problems


def _has(lines, text):
    return any(text in x for x in lines)


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


# ============================================================ Maestro over the mesh
@test("navicore.maestro_inventory", "NaviCore's hosted Maestro devices match W1's WDP row for WCB20 and W1's WCB20 proxies (read-only)", needs=["navicore", "wcb1"], links=[])
def maestro_inventory(bench):
    """NaviCore's own ?WDP,DUMP self row hard-codes MAESTRO=- (WCB_Mgmt.h:205-208), so the hosted set comes from
    GET_CONFIG. A W1 proxy for an id NaviCore does not host is stale and never evicted (CLAUDE.md rule 5): noted, not
    failed. Proxies are auto-added only for ids 1-8 (WCB_Maestro.cpp:735)."""
    nc, w = _nc(bench), usb_wcb(bench)
    local = {dev for _, dev in nc.local_slots(nc.config())}
    row = next((x for x in w.run("?WDP,DUMP", timeout=8) if x.startswith("[WDP:N=20,")), None)
    if row is None:
        time.sleep(60)
        row = next((x for x in w.run("?WDP,DUMP", timeout=8) if x.startswith("[WDP:N=20,")), None)
    assert row, "W1's WDP table has no WCB20 row"
    advertised = re.search(r"MAESTRO=([^,\]]*)", row).group(1)
    advertised_ids = set() if advertised == "-" else {int(x) for x in advertised.split(".")}
    proxies = {int(m.group(1)) for t in snapshot(bench, 1) for m in [re.match(r"^\?MAESTRO,M(\d+):W20S\d+:\d+$", t)] if m}
    stale = sorted(proxies - local)
    bench.note(f"NaviCore hosts Maestro devices {sorted(local)}; W1 WDP row MAESTRO={advertised}; W1 WCB20 proxies {sorted(proxies)}"
               + (f"; stale proxies {stale}" if stale else ""))
    assert advertised_ids == local, f"W1's row advertises {sorted(advertised_ids)}, NaviCore hosts {sorted(local)}"
    missing = sorted(d for d in local if 1 <= d <= 8 and d not in proxies)
    assert not missing, f"W1 has no WCB20 proxy for hosted devices {missing}"


@test("navicore.mae_cli_local", "?MAE query markers for an out-of-range, a disabled and each local slot (NaviCore USB)", needs=["navicore"], links=[])
def mae_cli_local(bench):
    """Never ?MAE,ERR (clears the error register), ?MAE,FREE (rewrites speed/accel) or a type-2 slot (broadcasts a
    query onto the mesh)."""
    nc = _nc(bench)
    cfg = nc.config()
    text = "\n".join(nc.cli("?MAE,GET,9,0") + nc.cli("?MAE,MOVING,0"))
    assert '[MAE:9]{"q":"pos","ch":0,"err":"disabled"}' in text and '[MAE:0]{"q":"mov","err":"disabled"}' in text, text
    disabled = next((i + 1 for i, m in enumerate(cfg.get("maestros", [])) if m.get("type") == 0), None)
    if disabled:
        text = "\n".join(nc.cli(f"?mae,get,{disabled},0"))
        assert f'[MAE:{disabled}]{{"q":"pos","ch":0,"err":"disabled"}}' in text, f"lowercase query on disabled slot {disabled}: {text!r}"
    for slot, _ in nc.local_slots(cfg):
        text = "\n".join(nc.cli(f"?MAE,MOVING,{slot}"))
        assert re.search(rf'\[MAE:{slot}\]\{{"q":"mov",("val":[01]|"err":"timeout")\}}', text), f"slot {slot}: {text!r}"


@test("navicore.maestro_mesh_settarget_readback", ";W20,;M<D>,setTarget from W1 moves NaviCore's own Maestro, not W1's; read back with ?MAE,GET", needs=["navicore", "wcb1"])
def maestro_mesh_settarget_readback(bench):
    w1s1 = link(bench, 1, "S1")
    nc, w = _nc(bench), usb_wcb(bench)
    slot, dev, ch, p0 = nc.undriven_channel(nc.config())
    target = 6400 if abs(p0 - 6400) > 200 else 5600
    bad = []
    with nc.debug(DBG_MAESTRO):
        try:
            nm, pm, wm = nc.dev.mark(), w1s1.mark(), w.dev.mark()
            w.send(f";W20,;M{dev},setTarget,{ch},{target}")
            time.sleep(3)
            dispatch = nc.lines(nm, rf"^\[DISPATCH\] Maestro slot {slot} \(device {dev}\) <- mesh  cmd 0x04$")
            if len(dispatch) != 1:
                bad.append(f"{len(dispatch)} dispatch lines for the setTarget")
            reached = False
            for _ in range(17):
                if nc.mae_get(slot, ch) == target:
                    reached = True
                    break
                time.sleep(0.3)
            if not reached:
                bad.append(f"channel {ch} never read back {target}")
            if _has(w.dev.since(wm), "is not a reachable target"):
                bad.append("W1 said WCB20 is unreachable")
            if w1s1.received(pm):
                bad.append("W1 put the ;M on its own Maestro port")
        finally:
            w.send(f";W20,;M{dev},setTarget,{ch},{p0}")
            time.sleep(2)
    assert not bad, "; ".join(bad)


@test("navicore.maestro_mesh_query_reply", ";W20,;MG<D>,1,<get> is answered by NaviCore with ;M!m<D>... into W1's RAM variables", needs=["navicore", "wcb1"], links=[])
def maestro_mesh_query_reply(bench):
    """Not W1's own ;M<D>,getPosition: queries are served local-port first, and W1's S1 holds only the probe."""
    nc, w = _nc(bench), usb_wcb(bench)
    slot, dev, pos = nc.usable_slot(nc.config())
    if not 1 <= dev <= 8:
        raise Skip(f"device {dev} is outside the m1..m8 names W1 accepts from ;M!")
    names = (f"m{dev}pos0", f"m{dev}moving")
    bad = []
    with nc.debug(DBG_MAESTRO):
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
        try:
            for verb, name, value in (("getPosition,0", names[0], pos), ("getMovingState", names[1], 0)):
                nm = nc.dev.mark()
                w.send(f";W20,;MG{dev},1,{verb}")
                time.sleep(1.2)
                replies = nc.lines(nm, rf"^\[DISPATCH\] Maestro {dev} <- mesh query")
                got = next((x.rstrip() for x in w.run(f"?VAR,GET,{name}") if x.startswith("[VAR]")), None)
                if f"[DISPATCH] Maestro {dev} <- mesh query -> WCB1: ;M!{name}={value}" not in [r.rstrip() for r in replies]:
                    bad.append(f"{verb}: NaviCore printed {replies}")
                if got != f"[VAR] {name} = {value}":
                    bad.append(f"{verb}: W1 {got!r}")
        finally:
            for n in names:
                w.run(f"?VAR,CLEAR,{n}")
    assert not bad, "; ".join(bad)


@test("navicore.maestro_mesh_rejects", "NaviCore's inbound ;M guards: variable push, malformed ;MG, bad replyTo, bad verb, unhosted device, the 47/48-character limit", needs=["navicore", "wcb1"], links=[])
def maestro_mesh_rejects(bench):
    nc, w = _nc(bench), usb_wcb(bench)
    local = {dev for _, dev in nc.local_slots(nc.config())}
    unhosted = next((d for d in range(1, 9) if d not in local), None)
    cases = [(";M!m1pos0=5", "[DISPATCH] Maestro: ignoring inbound variable push  ;M!m1pos0=5"),
             (";MG5", "[DISPATCH] Maestro: malformed ;MG  ;MG5"),
             (";MG1,25,getPosition,0", "[DISPATCH] Maestro: ;MG bad replyTo 25"),
             (";M1,bogus", "[DISPATCH] Maestro: unparseable inbound  ;M1,bogus"),
             (";M1,bogus" + "x" * 38, "[DISPATCH] Maestro: unparseable inbound  ;M1,bogus" + "x" * 38)]
    if unhosted:
        cases.append((f";MG{unhosted},1,getMovingState", f"[DISPATCH] Maestro: inbound query for device {unhosted} matches no local slot"))
    bad = []
    with nc.debug(DBG_MAESTRO):
        for i, (payload, want) in enumerate(cases):
            nm, wm = nc.dev.mark(), w.dev.mark()
            w.send(f";W20,{payload}")
            time.sleep(0.4)                 # let the mesh command reach NaviCore and be dispatched
            nc.dev.send("#L12")             # ...then flush: a lone [DISPATCH] line sits unsent on
            try:                            # NaviCore until more output follows it (see the header)
                nc.dev.expect(re.escape(want), timeout=3, since=nm)
            except AssertionError:
                bad.append(f"{payload[:24]}: no {want[:60]!r}")
            if _has(w.dev.since(wm), "is not a reachable target"):
                bad.append(f"{payload[:24]}: W1 refused to route it")
        nm = nc.dev.mark()
        w.send(";W20,;M1,bogus" + "x" * 39)           # 48 characters: dropped silently at enqueue (NaviCore.ino:347)
        time.sleep(2.0)
        if nc.lines(nm, r"^\[DISPATCH\] Maestro"):
            bad.append("a 48-character ;M was not dropped silently")
    assert not bad, "; ".join(bad)


@test("navicore.maestro_skip_not_logged_as_dispatch", "(should) An inbound ;M for a channel NaviCore skips (> 31) is not also logged as dispatched", needs=["navicore", "wcb1"], links=[])
def maestro_skip_not_logged_as_dispatch(bench):
    """Two problems with one command. The WcbCmd hosts fork: WcbMaestro (the WCB) accepts channels 0-127 and puts the
    frame on the wire, while NaviCore's maestroChanOk skips anything above 31 (NaviCore.ino:925-929), against the
    'same ;M, same bytes' premise. And maeInboundActuate logged '<- mesh  cmd 0x04' after the guard skipped the write
    (the wrappers are void, so the dlog could not tell), so the trace claimed a dispatch that never happened; the source
    now runs maestroChanOk before each channel-bearing case (NaviCore.ino:5081-5102), which needs a reflash. Exactly one of the two lines must appear: neither means the ;W20 never reached NaviCore or its debug
    output was lost, which must not pass; 'dispatched' alone is right if the guard is ever widened to 0-127."""
    nc, w = _nc(bench), usb_wcb(bench)
    slot, dev, _ = nc.usable_slot(nc.config())
    with nc.debug(DBG_MAESTRO):
        nm = nc.dev.mark()
        w.send(f";W20,;M{dev},setTarget,32,6000")
        time.sleep(1.5)
        lines = [x.rstrip() for x in nc.dev.since(nm)]
    skipped = f"[DISPATCH] Maestro {slot}: channel 32 out of range (0-31) — skipped" in lines
    claimed = f"[DISPATCH] Maestro slot {slot} (device {dev}) <- mesh  cmd 0x04" in lines
    bench.note(f"channel 32: skipped {skipped}, logged as dispatched {claimed}")
    assert not (skipped and claimed), "NaviCore logged a dispatch for a write its guard skipped"
    assert skipped != claimed, "NaviCore printed neither the skip nor the dispatch line: the ;M never reached it"


@test("navicore.maestro_mesh_fanout_0_9", ";W20,;M9 and ;W20,;M0 fire each local NaviCore Maestro once per distinct device", needs=["navicore", "wcb1"])
def maestro_mesh_fanout_0_9(bench):
    w1s1 = link(bench, 1, "S1")
    nc, w = _nc(bench), usb_wcb(bench)
    cfg = nc.config()
    slots = [(s, d, nc.mae_get(s, 0)) for s, d in nc.local_slots(cfg)]
    slots = [x for x in slots if isinstance(x[2], int)]
    if not slots:
        raise Skip("no local NaviCore Maestro answers")
    first_by_device = {}
    for s, d, _ in slots:
        first_by_device.setdefault(d, s)
    pfirst = slots[0][2]
    bad = []
    with nc.debug(DBG_MAESTRO):
        try:
            for fan in (9, 0):
                nm, pm = nc.dev.mark(), w1s1.mark()
                w.send(f";W20,;M{fan},setTarget,0,{pfirst}")
                time.sleep(2.0)
                lines = [x.rstrip() for x in nc.dev.since(nm)]
                fired = {(int(m.group(1)), int(m.group(2))) for x in lines for m in [re.match(r"^\[DISPATCH\] Maestro slot (\d+) \(device (\d+)\) <- mesh  cmd 0x04$", x)] if m}
                if fired != {(s, d) for d, s in first_by_device.items()}:
                    bad.append(f";M{fan}: fired {sorted(fired)}, expected {sorted((s, d) for d, s in first_by_device.items())}")
                if _has(lines, "matches no local slot"):
                    bad.append(f";M{fan}: a no-local-slot line")
                if w1s1.received(pm):
                    bad.append(f";M{fan}: W1 wrote its own Maestro port")
        finally:
            for s, _, p in slots:
                nc.mae_set(s, 0, p)
    assert not bad, "; ".join(bad)


@test("navicore.maestro_get_reply_mqr", "A plain ;M1,getPosition / getMovingState / getErrors that NaviCore sends W1 (WCB_SEND) is answered in the controller's format: W1 reads its local Maestro 1 (probe rules on W1 S1), stores m1pos0 / m1moving / m1err, and unicasts :MQR,1,0,POS,6000 / :MQR,1,0,MOV,1 / :MQR,1,0,ERR,4 to WCB20, which NaviCore's DBG_MAESTRO log shows arriving - never the ;M! form a WCB asker gets", needs=["navicore", "wcb1"], links=["W1S1"])
def maestro_get_reply_mqr(bench):
    """WCB-WP38 (maestro.mqr_reply_to_controller). The ETM receive path rewrites a standalone inbound ;M<dev>,get... to
    ;MG<dev>,<sender>,... (maestroRewriteInboundGet, WCB_Maestro.cpp:665-679, called at WCB.ino:5534), and
    handleMaestroGet reads a LOCAL slot first (:542-561, :583-596), stores the RAM variable (:607) and, for a replyTo
    that is the controller (WCB_SPECIAL_PEER_ID, 20 here), sends ':MQR,<dev>,<chan|0>,POS|MOV|ERR,<value>' instead of
    ';M!<var>=<value>' (:615-627). getMovingState is stored as 0/1 (:605). NaviCore logs the reply under DBG_MAESTRO
    before it parses it (NaviCore.ino:3053-3056) and keeps it only for a slot of type 2 (remote) carrying that device
    (maeConsumeRemoteReply, :796-812), which then surfaces as a [MAE:<slot>] marker (maePumpRemoteEmits, :817-835).
    A query moves nothing. W1 is Maestro_Remote, so a reply byte arriving after the 25 ms read would be bridged to W2's
    real Maestro 2: its error flags are read (cleared) at the end, as s22's _settle_maestro2 does (not imported: s22
    sorts after this suite, and importing it here would register its tests first)."""
    s1 = link(bench, 1, "S1")
    nc, w = _nc(bench), usb_wcb(bench)
    require_tokens(bench, 1, "?CONTROLLER,ON,20")
    if not any(re.match(r"^\?MAESTRO,M1:W1S1:\d+$", t, re.I) for t in snapshot(bench, 1)):
        raise Skip("W1 hosts no local Maestro 1 on S1")
    remote_slot = next((i + 1 for i, m in enumerate(nc.config().get("maestros", []))
                        if m.get("type") == 2 and m.get("device") == 1), None)
    cases = ((";M1,getPosition,0", "AA011000", "m1pos0", 6000, ":MQR,1,0,POS,6000", '{"q":"pos","ch":0,"val":6000}'),
             (";M1,getMovingState", "AA0113", "m1moving", 1, ":MQR,1,0,MOV,1", '{"q":"mov","val":1}'),
             (";M1,getErrors", "AA0121", "m1err", 4, ":MQR,1,0,ERR,4", '{"q":"err","val":4}'))
    bad, markers = [], []
    try:
        for case in cases:
            w.run(f"?VAR,CLEAR,{case[2]}")
        w.run("?DEBUG,MAESTRO,ON")
        s1.listen()
        s1.probe.rule_clear()
        s1.rule(1, "AA011000", bytes.fromhex("7017"))   # 0x1770 = 6000, low byte first
        s1.rule(2, "AA0113", bytes.fromhex("01"))       # moving
        s1.rule(3, "AA0121", bytes.fromhex("0400"))     # error register 4
        with nc.debug(DBG_MAESTRO):
            for cmd, frame, name, value, reply, marker_json in cases:
                wm, nm, pm = w.dev.mark(), nc.dev.mark(), s1.mark()
                ack = nc.wcb_send(1, cmd)
                if not ack.get("ok"):
                    bad.append(f"{cmd}: WCB_SEND answered {ack}")
                try:
                    w.dev.expect(rf"^\[MAESTRO\] get reply -> WCB20: {re.escape(reply)}$", timeout=3, since=wm)
                except AssertionError:
                    bad.append(f"{cmd}: W1 printed no 'get reply -> WCB20: {reply}'")
                time.sleep(0.4)
                nc.dev.send("#L12")         # releases a [DISPATCH] line NaviCore holds back (HIL_TESTING.md §5)
                try:
                    nc.dev.expect(re.escape(f"[DISPATCH] Maestro RX reply  {reply}"), timeout=3, since=nm)
                except AssertionError:
                    bad.append(f"{cmd}: NaviCore logged no '[DISPATCH] Maestro RX reply  {reply}'")
                if remote_slot:
                    want = f"[MAE:{remote_slot}]{marker_json}"
                    if not any(x.rstrip() == want for x in nc.dev.since(nm)):
                        bad.append(f"{cmd}: NaviCore, with remote slot {remote_slot} on device 1, printed no {want}")
                    markers.append(want)
                lines = [x.rstrip() for x in w.dev.since(wm)]
                if any(x.startswith("[MAESTRO] get reply -> WCB20: ;M!") for x in lines):
                    bad.append(f"{cmd}: W1 answered the controller in the ;M! form")
                got_frame = s1.received(pm)
                if got_frame != bytes.fromhex(frame):
                    bad.append(f"{cmd}: W1 S1 got {got_frame.hex(' ') or 'nothing'}, expected exactly {frame}")
                got = next((x.rstrip() for x in w.run(f"?VAR,GET,{name}") if x.startswith("[VAR]")), None)
                if got != f"[VAR] {name} = {value}":
                    bad.append(f"{cmd}: W1 stored {got!r}, expected {name} = {value}")
    finally:
        s1.probe.rule_clear()
        for case in cases:
            w.run(f"?VAR,CLEAR,{case[2]}")
        w.run("?DEBUG,MAESTRO,OFF")
        w.send(";M2,getErrors")             # read (and so clear) Maestro 2's error flags
        time.sleep(1.5)
        w.run("?VAR,CLEAR,m2err")
    bench.note("NaviCore " + (f"has remote slot {remote_slot} on device 1: markers {markers}" if remote_slot else
                              "has no remote Maestro slot on device 1, so it logs each :MQR and keeps nothing"))
    assert not bad, "; ".join(bad)


@test("navicore.serial_route_dbg", ";W20,;s<n> from W1 reaches NaviCore's aux transmitter (seen through DBG_SERIAL); S0 and S4 are refused", needs=["navicore", "wcb1"], links=[])
def serial_route_dbg(bench):
    """The bytes are physically written to NaviCore's S3-S5; a targeted write is never skipped even when a device owns
    the port (auxPortHasDevice, NaviCore.ino:2946-2956), so the test skips when HCR/MP3/DFPlayer/WLED is routed to those
    ports. Until 2026-09-28 the check read keys 'mp3', 'dfp' and 'wled', which GET_CONFIG never has, and 'hcrDest',
    which it always has, so it only ever noted (NAVICORE.md D-NC15); it now reads the routing the firmware reads."""
    nc, w = _nc(bench), usb_wcb(bench)
    cfg = nc.config()
    local = nc.local_devices(cfg)
    if local:
        raise Skip(f"NaviCore routes {', '.join(local)} to its own aux ports: this test's text would reach them")
    labels = ["Serial 3", "Serial 4", "Serial 5"] if cfg.get("boardType") == 1 else ["Serial 1", "Serial 2", "Serial 3"]
    tag = nonce()
    sends = [(f";s1HILA{tag}", f"[DISPATCH] Serial TX [{labels[0]}]  HILA{tag}"), (f";S2HILB{tag}", f"[DISPATCH] Serial TX [{labels[1]}]  HILB{tag}"),
             (f";s3HILC{tag}", f"[DISPATCH] Serial TX [{labels[2]}]  HILC{tag}"),
             (f";s4HILD{tag}", "[DISPATCH] Serial route: S4 is not a NaviCore port (S1-S3)"),
             (f";s0HILE{tag}", "[DISPATCH] Serial route: S0 is not a NaviCore port (S1-S3)")]
    bad = []
    with nc.debug(DBG_SERIAL):
        for payload, want in sends:
            nm = nc.dev.mark()
            w.send(f";W20,{payload}")
            time.sleep(1.2)
            if want not in [x.rstrip() for x in nc.dev.since(nm)]:
                bad.append(f"{payload[:5]}: no {want!r}")
    assert not bad, "; ".join(bad)


# ============================================================ broadcasts, WCB_SEND and bridged JSON
def _expected_ports(bench):
    """{link key: (link, receives a plain broadcast)} from each board's config: broadcast OUT on, and not a Maestro /
    MP3 / DFPlayer / HCR / PWM-output port, not S1 under ?MAESTRO,REMOTE, not the Kyber's own port under Kyber local.
    Only that one port: processBroadcastCommand (WCB.ino) skips kyberLocalPort, and the other hardware port follows its
    ?BCAST flags like any other (tracker #14). The backup always writes the S form, ?KYBER,LOCAL,S<n>[,targets]; a bare
    ?KYBER,LOCAL keeps the current Kyber port, S2 on a board that is not Kyber local (storeKyberSettings,
    WCB_Storage.cpp), which is the fallback below. A WLED port is covered by its OUT flag, which configuring WLED turns
    off (WCB_WLED.cpp:164-172)."""
    out = {}
    for wcb in (1, 2):
        tokens = [t.upper() for t in snapshot(bench, wcb)]
        kyber = next((t for t in tokens if t.startswith("?KYBER,LOCAL")), None)
        kport = None if kyber is None else "S" + (re.match(r"^\?KYBER,LOCAL(?:,S(\d))?", kyber).group(1) or "2")
        for p in ("S1", "S2", "S3", "S4", "S5"):
            l = bench.links.get(wcb, p)
            if not l:
                continue
            device = any(re.search(rf"(?:\b|W{wcb}){p}\b", t) for t in tokens
                         if t.startswith(("?MAESTRO,M", "?MP3,", "?DFP,", "?HCR,PORT", "?MAP,PWM,OUT")))
            reserved = ("?MAESTRO,REMOTE" in tokens and p == "S1") or p == kport
            out[l.key] = (l, f"?BCAST,OUT,{p},ON" in tokens and not device and not reserved)
    return out


def _port_copies(expected, watch, text):
    bad = []
    for key, (l, receives) in expected.items():
        n = watch.got(l).count(text.encode() + b"\r")
        if receives and n != 1:
            bad.append(f"{key}: {n} copies, expected 1")
        if not receives and n:
            bad.append(f"{key}: {n} copies, expected silence")
    return bad


@test("navicore.plain_broadcast_both_ways", "Plain text broadcast from W1 reaches NaviCore; NaviCore's WCB_SEND target-0 text reaches every eligible WCB port exactly once", needs=["navicore", "wcb1"], links=["W1S1|W1S2|W2S1|W2S2|W2S3"])   # any wired port is watched; at least one is needed
def plain_broadcast_both_ways(bench):
    nc, w = _nc(bench), usb_wcb(bench)
    expected = _expected_ports(bench)
    if not expected:
        raise Skip("no wired WCB port to watch")
    links = [l for l, _ in expected.values()]
    t_a, t_b = f"HILB{nonce()}", f"HILP{nonce()}"
    bad = []
    with nc.debug(DBG_MAESTRO):                 # the [WCB RX] fallback line is DBG_MAESTRO-gated (NaviCore.ino:3093-3106)
        watch, nm = Watch(*links), nc.dev.mark()
        w.send(t_a)
        time.sleep(3)
        if f"[WCB RX] from WCB1: {t_a}" not in [x.rstrip() for x in nc.dev.since(nm)]:
            bad.append("NaviCore did not log W1's broadcast")
        bad += [f"W1 broadcast, {x}" for x in _port_copies(expected, watch, t_a)]
        watch = Watch(*links)
        ack = nc.ack_line({"type": "WCB_SEND", "target": 0, "cmd": t_b})
        time.sleep(3)
        if ack != '{"type":"ACK","ok":true}':
            bad.append(f"WCB_SEND ACK {ack}")
        bad += [f"NaviCore broadcast, {x}" for x in _port_copies(expected, watch, t_b)]
    assert not bad, "; ".join(bad)


@test("navicore.wcb_send_broadcast_exec", "NaviCore's WCB_SEND target 0 with ;S3 runs once on every WCB", needs=["navicore", "wcb1"])
def wcb_send_broadcast_exec(bench):
    s12, s13, s23 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 2, "S3")
    nc = _nc(bench)
    t = f"HILW{nonce()}"
    watch = Watch(s12, s13, s23)
    ack = nc.ack_line({"type": "WCB_SEND", "target": 0, "cmd": f";S3{t}"}, timeout=1.5)
    watch.expect(s13, t.encode() + b"\r", timeout=3)
    watch.expect(s23, t.encode() + b"\r", timeout=3)
    time.sleep(2)
    assert ack == '{"type":"ACK","ok":true}', ack
    for l in (s13, s23):
        assert watch.got(l).count(t.encode() + b"\r") == 1, f"{l.key}: {watch.got(l)!r}"
        assert not l.errors(watch.marks[l.key]), f"RXERR on {l.key} (a soft-serial mis-frame under mesh load is a real finding)"
    assert not watch.got(s12), "the ;S3 broadcast reached W1 S2"


@test("navicore.wcb_send_edges", "WCB_SEND: out-of-range targets are rejected; a 188-character broadcast is refused by the library; 187 characters are delivered", needs=["navicore", "wcb1"])
def wcb_send_edges(bench):
    """Never omit "target": it defaults to 0, a broadcast."""
    s12, s13, s23 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 2, "S3")
    nc = _nc(bench)
    bad = []
    watch = Watch(s12)
    for target in (21, -1):
        ack = nc.ack_line({"type": "WCB_SEND", "target": target, "cmd": f";S2HILX{nonce()}"})
        if ack != f'{{"type":"ACK","ok":false,"msg":"target {target} out of range (0=broadcast, 1-20=unicast)"}}':
            bad.append(f"target {target}: {ack}")
    time.sleep(2)
    if watch.got(s12):
        bad.append("a rejected WCB_SEND reached W1 S2")
    long_cmd, fits = ";S3" + padded("L", 185), ";S3" + padded("K", 184)
    watch, nm = Watch(s13, s23), nc.dev.mark()
    nc.ack_line({"type": "WCB_SEND", "target": 0, "cmd": long_cmd})
    time.sleep(3)
    if not _has(nc.dev.since(nm), "[WCB_Client] broadcast: command too long (188 > 187 chars)"):
        bad.append("no library refusal for 188 characters")
    if any(long_cmd[3:].encode() in watch.got(l) for l in (s13, s23)):
        bad.append("the 188-character broadcast was delivered")
    watch = Watch(s13, s23)
    ack = nc.ack_line({"type": "WCB_SEND", "target": 0, "cmd": fits})
    try:
        for l in (s13, s23):
            watch.expect(l, fits[3:].encode() + b"\r", timeout=3)
    except AssertionError:
        bad.append("the 187-character broadcast was not delivered to both WCBs")
    if ack != '{"type":"ACK","ok":true}':
        bad.append(f"187-character ACK {ack}")
    assert not bad, "; ".join(bad)


@test("navicore.wcb_send_ack_reflects_refusal", "(should) WCB_SEND answers ok:false for a broadcast the library refused (188 characters under checksums)", needs=["navicore"], links=[])
def wcb_send_ack_reflects_refusal(bench):
    """NaviCore's USB WCB_SEND discards the return value of wcb->broadcast()/send() and always replies ok:true
    (NaviCore.ino:4035-4040 vs WCB_Client.cpp:388-393), so the config tool reports a refused broadcast as sent."""
    nc = _nc(bench)
    ack = nc.ack_line({"type": "WCB_SEND", "target": 0, "cmd": ";S3" + padded("R", 185)})
    assert '"ok":false' in ack, f"a refused broadcast was acknowledged: {ack}"


@test("navicore.wcb_send_fragmented_unicast", "A 300-character WCB_SEND unicast is fragmented by the library and runs exactly once on W1", needs=["navicore", "wcb1"])
def wcb_send_fragmented_unicast(bench):
    s12 = link(bench, 1, "S2")
    nc = _nc(bench)
    cmd = ";S2" + padded("F", 297)
    m, nm = s12.mark(), nc.dev.mark()
    ack = nc.ack_line({"type": "WCB_SEND", "target": 1, "cmd": cmd})
    s12.expect(cmd[3:].encode() + b"\r", timeout=3, since=m)
    time.sleep(5)
    lines = nc.dev.since(nm)
    assert ack == '{"type":"ACK","ok":true}', ack
    assert _has(lines, "[WCB_Client] send: 300 chars > single-packet limit — fragmenting to WCB1 (2 chunks, session "), "no fragmenting line"
    assert any(re.search(r"\[WCB_Client\] fragmented send to WCB1 complete \(session [0-9A-Fa-f]+, 2 chunks x 3 passes\)", x) for x in lines), "no completion line"
    assert s12.received(m).count(cmd[3:].encode() + b"\r") == 1, "the fragmented command ran more than once"


@test("navicore.bridged_json_ack", "JSON bridged through W1 (;W20,{...}): PING, WCB_SEND ok and bad, TEST_ACTION and GET_MESH_STATS answer back as sys JSON on W1 USB", needs=["navicore", "wcb1"])
def bridged_json_ack(bench):
    """NaviCore parks one bridged WCB_SEND and one TEST_ACTION at a time, so requests go at least 200 ms apart
    (rc_telemetry.h:2433-2440). The ;W20,{json} also opens W1's 20 s relay window (WCB.ino:6625-6627)."""
    s23 = link(bench, 2, "S3")
    nc, w = _nc(bench), usb_wcb(bench)
    fw = nc.ping()
    tm, tn, ty = f"HILM{nonce()}", f"HILN{nonce()}", f"HILY{nonce()}"
    bad = []

    def bridged(payload, pattern, timeout=3.0):
        wm = w.dev.mark()
        w.send(f";W20,{payload}")
        try:
            w.dev.expect(pattern, timeout=timeout, since=wm)
            return True
        except AssertionError:
            return False

    with nc.debug(DBG_WCB):
        if not bridged('{"type":"PING"}', rf'\{{"sys":1,"type":"PONG","id":20,"version":"{re.escape(fw)}"'):
            bad.append("no bridged PONG")
        m = s23.mark()
        if not bridged(f'{{"type":"WCB_SEND","target":2,"cmd":";S3{tm}"}}', r'\{"sys":1,"type":"ACK","of":"WCB_SEND","ok":true\}'):
            bad.append("no bridged WCB_SEND ok ACK")
        time.sleep(3)
        if tm.encode() + b"\r" not in s23.received(m):
            bad.append("the bridged WCB_SEND did not reach W2 S3")
        m = s23.mark()
        if not bridged(f'{{"type":"WCB_SEND","target":25,"cmd":";S3{tn}"}}', r'\{"sys":1,"type":"ACK","of":"WCB_SEND","ok":false\}'):
            bad.append("no bridged WCB_SEND ok:false for target 25")
        time.sleep(2)
        if tn.encode() in s23.received(m):
            bad.append("target 25 still delivered")
        m, nm = s23.mark(), nc.dev.mark()
        if not bridged(f'{{"type":"TEST_ACTION","action":{{"type":"wcb_unicast","target":"2","cmd":";S3{ty}"}}}}',
                       r'\{"sys":1,"type":"ACK","of":"TEST_ACTION","ok":true\}'):
            bad.append("no bridged TEST_ACTION ACK")
        time.sleep(2)
        if f"[DISPATCH] WCB→2  ;S3{ty}" not in [x.rstrip() for x in nc.dev.since(nm)] or ty.encode() + b"\r" not in s23.received(m):
            bad.append("the bridged TEST_ACTION did not dispatch to W2 S3")
        wm = w.dev.mark()
        w.send(';W20,{"type":"GET_MESH_STATS"}')
        try:
            w.dev.expect(r'"last":1', timeout=5, since=wm)
            pages = [x for x in w.dev.since(wm) if '"type":"MESH_STATS"' in x and '"sys":1' in x]
            if not pages or '"self":20' not in pages[0] or '"agg":{' not in pages[0]:
                bad.append(f"bridged MESH_STATS pages {pages[:1]}")
        except AssertionError:
            bad.append("no final bridged MESH_STATS page")
    assert not bad, "; ".join(bad)


@test("navicore.mesh_json_declined", "Declined JSON from the mesh (unknown type, no type, a peer's rc_*, unparseable) is logged but never fanned out to NaviCore's aux ports", needs=["navicore", "wcb1"], links=[])
def mesh_json_declined(bench):
    nc, w = _nc(bench), usb_wcb(bench)
    tag = nonce()
    cases = [(f'{{"type":"HILX{tag}"}}', [f"[RC] Unknown inbound type 'HILX{tag}' from WCB1", f'[WCB RX] from WCB1: {{"type":"HILX{tag}"}}'], ["xx-none"]),
             ('{"hil":1}', ['[WCB RX] from WCB1: {"hil":1}'], ["[RC] Unknown inbound type"]),
             ('{"type":"rc_hil","id":5}', [], ["rc_hil"]),
             (f"{{HILbad{tag}", [f"[WCB RX] from WCB1: {{HILbad{tag}"], ["[RC] Unknown inbound type"])]
    bad = []
    with nc.debug(DBG_MAESTRO | DBG_SERIAL):
        for payload, wants, not_wants in cases:
            nm = nc.dev.mark()
            w.send(f";W20,{payload}")
            time.sleep(2)
            lines = [x.rstrip() for x in nc.dev.since(nm)]
            bad += [f"{payload}: no {x!r}" for x in wants if x not in lines]
            bad += [f"{payload}: unexpected {x!r}" for x in not_wants if _has(lines, x)]
            if _has(lines, "[DISPATCH] Serial TX"):
                bad.append(f"{payload}: fanned out to an aux port")
    assert not bad, "; ".join(bad)


@test("navicore.mesh_stats_counts", "After RESET_MESH_STATS, NaviCore's per-peer sent/ackd for unicasts and an ensured broadcast, and per-sender recv, are exact", needs=["navicore", "wcb1"], links=[],
      drives=["W1S2", "W1S3", "W2S3"])   # WCB_SEND ;S2 to W1, and an ensured broadcast of ;S3 that every WCB writes
def mesh_stats_counts(bench):
    """Exact only while nothing else sends from NaviCore in the ~7 s window, and NaviCore sends two periodic tracked
    unicasts of its own (tracker #76): its mesh-stats report to statsReport.wcb every 30 s (reportMeshStats,
    rc_telemetry.h), and ;V,MODE to modeReport.wcb every 60 s, re-timed by every mode change. On this bench those are W1
    and W2, and a report inside the window failed this test in full run 20260922-215719 (W1 sent 5, not 4). W1 prints the
    last stats report's age (?STATS 'Reported by Other Nodes'), so the window starts right after one. The mode report's
    phase can't be read, so its target may show exactly one extra send. ;W20,?version replies travel as RTERM raw
    packets and add nothing to 'sent'."""
    nc, w = _nc(bench), usb_wcb(bench)
    cfg = nc.config()

    def _report_to(key):
        r = cfg.get(key) or {}
        return r.get("wcb") if r.get("enabled") else None
    stats_to, mode_to = _report_to("statsReport"), _report_to("modeReport")
    if stats_to == 1:
        for _ in range(2):
            age = next((int(mm.group(1)) for x in w.run("?STATS")
                        for mm in [re.match(r"^WCB20: Sent: .*\((\d+)s ago\)$", x.rstrip())] if mm), None)
            if age is None or age < 20:   # none yet this boot, or the next is >= 10 s away
                break
            time.sleep(31 - age)          # let the next one land, then start the window
    st = nc.wcb_status()
    known = {i + 1 for i, v in enumerate(st.get("known", [])) if v}
    online = {i + 1 for i, v in enumerate(st.get("online", [])) if v}
    m = nc.dev.mark()
    reset = nc.ack_line({"type": "RESET_MESH_STATS"})
    nc.dev.expect(r"\[WCB\] mesh stats cleared", timeout=3, since=m)
    for i in range(3):
        nc.ack_line({"type": "WCB_SEND", "target": 1, "cmd": f";S2HILS{i}{nonce()}"})
        time.sleep(0.3)
    nc.ack_line({"type": "WCB_SEND", "target": 0, "cmd": f";S3HILS{nonce()}"})
    for _ in range(2):
        wm = w.dev.mark()
        w.send(";W20,?version")
        w.dev.expect(r"^\[TERM:20\]Software Version:", timeout=5, since=wm)
    time.sleep(3)
    stats = nc.mesh_stats()
    pages, rows, agg = stats["pages"], stats["peers"], stats["agg"]
    bench.note(f"NaviCore mesh stats rows {rows}, agg {agg}")
    assert reset == '{"type":"ACK","of":"RESET_MESH_STATS","ok":true}', reset
    assert pages and pages[0].get("self") == 20, "no MESH_STATS page 0 for self 20"
    assert set(rows) == {i for i in known if i != 20}, f"rows for {sorted(rows)}, known {sorted(known)}"
    r1 = rows.get(1)
    w1_sent = (4, 5) if mode_to == 1 else (4,)
    assert r1 and r1[1] in w1_sent and r1[4] == 0 and r1[2] + r1[5] == r1[1] and r1[6] == 2, \
        f"W1 row [id,sent,ackd,rty,fail,ung,recv] = {r1} (expected sent {w1_sent}, fail 0, ackd+ung = sent, recv 2)"
    for i in sorted(online - {1, 20}):
        if i in rows and st.get("clients", [0] * 20)[i - 1] == 0:
            sent_ok = (1, 2) if i in (mode_to, stats_to) else (1,)
            assert rows[i][1] in sent_ok and rows[i][2] == rows[i][1], \
                f"WCB{i} row {rows[i]} (expected the one broadcast{' plus at most one periodic report' if len(sent_ok) > 1 else ''}, all ACKed)"
    assert agg.get("recv", 0) >= 2 and agg.get("bcast", 0) >= 1, agg


@test("navicore.forget_peer_safe", "FORGET_PEER / ?FORGET on ids that can never be learned (1, 20) and bad arguments change nothing", needs=["navicore", "wcb1"], links=[])
def forget_peer_safe(bench):
    """Minor finding: FORGET_PEER over USB answers ok:true for an id that was not a learned peer, and the mesh form
    sends no ACK, so only the console line tells 'forgotten' from 'nothing to forget' (NaviCore.ino:4060-4062,
    rc_telemetry.h:2416-2422). NEVER an id NaviCore has learned, nor all:true / ?FORGET,ALL: those rewrite its NVS."""
    nc, w = _nc(bench), usb_wcb(bench)
    known0 = nc.wcb_status().get("known")
    peers0 = {r["N"]: r.get("PEER") for r in nc.wdp_dump()}
    bad = []
    for pid in (1, 20):
        m = nc.dev.mark()
        ack = nc.ack_line({"type": "FORGET_PEER", "id": pid})
        if f"[WCB] WCB {pid} is not a learned peer (nothing to forget)" not in [x.rstrip() for x in nc.dev.since(m)]:
            bad.append(f"id {pid}: no 'not a learned peer' line")
        if ack != f'{{"type":"ACK","of":"FORGET_PEER","ok":true,"id":{pid}}}':
            bad.append(f"id {pid}: ACK {ack}")
    ack0 = nc.ack_line({"type": "FORGET_PEER", "id": 0})
    if ack0 != '{"type":"ACK","of":"FORGET_PEER","ok":false,"msg":"id 0 out of range (1-20) or set all:true"}':
        bad.append(f"id 0: {ack0}")
    for cmd, want in (("?FORGET,abc", "[WCB] usage: ?FORGET,<id 1-20>  or  ?FORGET,ALL"), ("?FORGET,21", "[WCB] usage: ?FORGET,<id 1-20>  or  ?FORGET,ALL"),
                      ("?FORGET,1", "[WCB] WCB 1 is not a learned peer (nothing to forget)")):
        m = nc.dev.mark()
        nc.dev.send(cmd)
        try:
            nc.dev.expect(re.escape(want), timeout=2, since=m)
        except AssertionError:
            bad.append(f"{cmd}: no {want!r}")
    m = nc.dev.mark()
    w.send(';W20,{"type":"FORGET_PEER","id":20}')
    try:
        nc.dev.expect(re.escape("[WCB] WCB 20 is not a learned peer (nothing to forget)"), timeout=3, since=m)
    except AssertionError:
        bad.append("the mesh FORGET_PEER printed nothing")
    if nc.wcb_status().get("known") != known0 or {r["N"]: r.get("PEER") for r in nc.wdp_dump()} != peers0:
        bad.append("NaviCore's known peers or WDP PEER flags changed")
    assert not bad, "; ".join(bad)


@test("navicore.set_mode", "SET_MODE is mesh-only: unknown over USB; over the mesh it sets the mode, never re-emits rc_mode on a repeat or a bad mode, and holds while the SBUS mode switch is still (the first rc_mode is a best-effort broadcast: counted, not asserted)", needs=["navicore", "wcb1"], links=[])
def set_mode(bench):
    """Comment/code gap: rc_telemetry.h's SET_MODE comment says the next SBUS frame overwrites it, but processSbus only
    rewrites the mode when the bound channel moves more than 5 counts (NaviCore.ino:2779), so a mesh SET_MODE holds.
    resetModeAwareKnobs() re-arms mode-aware knobs, so their servos follow the stick in the new mode."""
    nc, w = _nc(bench), usb_wcb(bench)
    m0 = nc.mode()
    m1 = next(x for x in (1, 2, 3) if x != m0)
    bad = []
    try:
        usb = nc.json_cmd({"type": "SET_MODE", "mode": m1}, r'^\{"type":"ERROR"', timeout=2).string
        if '"msg":"unknown type"' not in usb or nc.mode() != m0:
            bad.append(f"USB SET_MODE: {usb}")
        wm = w.dev.mark()
        w.send(f';W20,{{"type":"SET_MODE","mode":{m1}}}')
        time.sleep(2)
        if nc.mode() != m1:
            bad.append("the mesh SET_MODE did not change the mode")
        emitted = sum(f'"type":"rc_mode","id":20,"mode":{m1}' in x for x in w.dev.since(wm))
        bench.note(f"rc_mode lines relayed to W1 after SET_MODE: {emitted} (best-effort broadcast)")
        wm = w.dev.mark()
        w.send(f';W20,{{"type":"SET_MODE","mode":{m1}}}')
        time.sleep(1.5)
        if _has(w.dev.since(wm), '"type":"rc_mode"'):
            bad.append("a repeated SET_MODE emitted rc_mode again")
        wm = w.dev.mark()
        w.send(';W20,{"type":"SET_MODE","mode":4}')
        time.sleep(1.5)
        if nc.mode() != m1 or _has(w.dev.since(wm), '"type":"rc_mode"'):
            bad.append("mode 4 was accepted")
        time.sleep(5)
        if nc.mode() != m1:
            bad.append("the SBUS mode switch overrode the mesh SET_MODE while still")
    finally:
        w.send(f';W20,{{"type":"SET_MODE","mode":{m0}}}')
        time.sleep(2)
    assert nc.mode() == m0, "the original mode was not restored"
    assert not bad, "; ".join(bad)


@test("navicore.test_action_usb", "USB TEST_ACTION fires wcb_unicast, wcb_broadcast and serial actions with exact dispatch lines and bytes on the wire", needs=["navicore", "wcb1"])
def test_action_usb(bench):
    """TEST_ACTION bypasses the calibration and replay gates; never record/play/stop. The serial action writes
    NaviCore's first aux port."""
    s12, s13, s23 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 2, "S3")
    nc = _nc(bench)
    label = "Serial 3" if nc.config().get("boardType") == 1 else "Serial 1"
    tag = nonce()
    acks = []
    with nc.debug(DBG_WCB | DBG_SERIAL):
        watch, nm = Watch(s12, s13, s23), nc.dev.mark()
        acks.append(nc.ack_line({"type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": "1", "cmd": f";S2HILT{tag}"}}))
        watch.expect(s12, f"HILT{tag}\r".encode(), timeout=3)
        acks.append(nc.ack_line({"type": "TEST_ACTION", "action": {"type": "wcb_broadcast", "cmd": f";S3HILU{tag}"}}))
        watch.expect(s13, f"HILU{tag}\r".encode(), timeout=3)
        watch.expect(s23, f"HILU{tag}\r".encode(), timeout=3)
        acks.append(nc.ack_line({"type": "TEST_ACTION", "action": {"type": "serial", "port": "S3", "cmd": f"HILV{tag}"}}))
        time.sleep(1.5)
        lines = [x.rstrip() for x in nc.dev.since(nm)]
    ok = '{"type":"ACK","of":"TEST_ACTION","ok":true}'
    assert acks == [ok, ok, ok], acks
    for want in (f"[DISPATCH] WCB→1  ;S2HILT{tag}", f"[DISPATCH] WCB broadcast  ;S3HILU{tag}", f"[DISPATCH] Serial TX [{label}]  HILV{tag}"):
        assert want in lines, f"no {want!r}"
    assert watch.got(s13).count(f"HILU{tag}\r".encode()) == 1 and watch.got(s23).count(f"HILU{tag}\r".encode()) == 1, "broadcast copies"
    assert not _has(lines, "rc_trig"), "TEST_ACTION went through rcDispatch"


@test("navicore.test_action_rejects", "TEST_ACTION with no action, a retired type, a bad board id, or truncated JSON does nothing", needs=["navicore", "wcb1"])
def test_action_rejects(bench):
    s12 = link(bench, 1, "S2")
    nc = _nc(bench)
    no = '{"type":"ACK","of":"TEST_ACTION","ok":false}'
    bad = []
    with nc.debug(DBG_WCB):
        for obj in ({"type": "TEST_ACTION"}, {"type": "TEST_ACTION", "action": {"type": "smooth"}}):
            if nc.ack_line(obj) != no:
                bad.append(f"{obj} was accepted")
        watch, nm = Watch(s12), nc.dev.mark()
        for target in ("25", "abc"):
            nc.ack_line({"type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": target, "cmd": f";S2HILZ{nonce()}"}})
        time.sleep(2)
        if _has(nc.dev.since(nm), "[DISPATCH] WCB") or watch.got(s12):
            bad.append("a bad board id dispatched")
        m = nc.dev.mark()
        nc.dev.send('{"type":"TEST_ACTION","action":')
        try:
            err = nc.dev.expect(r'^\{"type":"ERROR","msg":"JSON parse failed \(', timeout=3, since=m).string
            if '"rxLen":' not in err:
                bad.append(f"truncated JSON error lacks rxLen: {err}")
        except AssertionError:
            bad.append("truncated JSON got no parse error")
    assert not bad, "; ".join(bad)


@test("navicore.test_action_bad_target_not_ok", "(should) TEST_ACTION answers ok:false for a wcb_unicast whose board id is outside 1-20", needs=["navicore"], links=[])
def test_action_bad_target_not_ok(bench):
    """rcExecuteActionNow silently sends nothing for a board id outside 1-20 (NaviCore.ino:2043-2052), but TEST_ACTION
    still answers ok:true (NaviCore.ino:2144-2150), so the config tool's Test button reports success for nothing."""
    nc = _nc(bench)
    ack = nc.ack_line({"type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": "25", "cmd": f";S2HILZ{nonce()}"}})
    assert '"ok":false' in ack, f"an action that did nothing was acknowledged: {ack}"


# ============================================================ the Wizard's config pull through NaviCore's relay (F13)
# NaviCore answers ?MGMT,PULL on USB through the WCB_Client library's relay (WcbMgmt, WCB_Mgmt.h: sendConfigReq, then
# processConfigFrag from service() on the loop task), so it is the bench's WCB_Client relay - and, until it is rebuilt
# on a library with F13, its OLD relay. A library from before F13 reads "2,P" with atoi as 2, sends only the plain
# request (type 5), and drops the target's packet type 18 (parts and refusals) unseen; a newer one sends the parts
# request (type 19) for ",P" and prints [MGMT:CFGPART,n] / [MGMT:CFGERR,n]. W2, the target, is read on its own USB.
# The replies carry the mesh password: only lengths, counts and codes are noted.
@test("navicore.mgmt_pull", "NaviCore's WCB_Client relay pulls W2's config as the one [MGMT:CONFIG,2] line, equal to W2's own factory chain, both for ?MGMT,PULL,2 and for the new Wizard's ?MGMT,PULL,2,P (F13)", needs=["navicore", "wcb2"], links=[])
def mgmt_pull_via_navicore(bench):
    """Under 2912 characters every target sends the one legacy line, whatever the request asked for, so this holds for
    an old library and a new one alike. The requester is NaviCore (WCB 20), so W2's per-requester dedup is its own."""
    nc, w2 = _nc(bench), WCB(bench.dev("wcb2"))
    want = _factory_reply(w2, w2.version())
    assert len(want) <= PULL_MAX, (f"W2's pull reply is {len(want)} characters; this test needs a one-line config "
                                   f"(<= {PULL_MAX}) - a leftover from an aborted size test?")
    problems = []
    for parts in (False, True):
        r = pull_config(nc.dev, 2, parts=parts, timeout=10, verify=False)
        form = "?MGMT,PULL,2,P" if parts else "?MGMT,PULL,2"
        problems += [f"{form}: {p}" for p in _reply_problems(r, want, [], "legacy", 1)]
    bench.note(f"NaviCore pulled W2's {len(want)}-character config as one line, plain and with ,P")
    assert not problems, "; ".join(problems)


# W2 prints this under ?DEBUG,MGMT when it accepts a config request, with ' (parts accepted)' for a parts request
# (type 19) - cpjStart, WCB.ino. NaviCore is WCB 20 (every ;W20 in this file).
NC_PARTS_ACCEPTED = re.compile(r"\[MGMT\] Config request from WCB20 \(parts accepted\)")
# What a ,P pull through a library with F13 may end in besides parts: a refusal that passes on its own. Named here, not
# taken from RETRYABLE (hil/wcb.py): that is read_config's retry policy and holds NOPARTS, while a ,P pull through a
# working relay never ends in NOPARTS, because Pull asks again after one.
NC_PARTS_PASS = ("refused NOMEM", "refused CHANGED")


def _navicore_pull_outcome(nc, parts, n, want, problems, timeout=8):
    """Pull W2 (over the limit) through NaviCore -> what the pull ended in: 'silent' (nothing within `timeout`),
    'refused <code>', 'parts K' or 'a config line', after 'refused NOPARTS, then ' when Pull asked a ,P pull again
    after a NOPARTS (every type-19 copy lost - it happens; see hil/wcb.py Pull). Judged here is what is wrong whichever
    library answers: a [MGMT:CONFIG,2] line, which an old Wizard would store as W2's whole config; parts for a plain
    pull, or parts that do not join into W2's chain; a plain pull refused for anything but NOPARTS. What a ,P pull must
    end in depends on the library, so _navicore_library_problems judges that."""
    form = "?MGMT,PULL,2,P" if parts else "?MGMT,PULL,2"
    p = Pull(nc.dev, 2, parts=parts, timeout=timeout)
    outcome = "silent"
    try:
        while not p.poll():
            time.sleep(0.05)
        r = p.reply(verify=False)
        outcome = f"parts {r.count}" if r.kind == "parts" else "a config line"
        if r.kind == "parts":
            if not parts:
                problems.append(f"{form}: {r.count} parts for a request that did not ask for them")
            problems += [f"{form}: {x}" for x in _reply_problems(r, want, [], "parts")]
    except PullRefused as e:
        outcome = f"refused {e.code}"
        if not parts and e.code != "NOPARTS":           # a plain pull over the limit is refused NOPARTS, only
            problems.append(f"{form}: CFGERR {e.code} ({e.detail})")
    except AssertionError:
        pass                                        # nothing in time: a relay that drops packet type 18
    if p.resent:
        outcome = f"refused {p.resent[0]}, then {outcome}"
    time.sleep(1.0)
    lines = _pull_lines(nc.dev, p.mark, 2)["CONFIG"]
    if lines:
        problems.append(f"{form}: NaviCore printed {len(lines)} [MGMT:CONFIG,2] line(s) (bodies of {lines} characters) "
                        f"for W2's {n}-character config")
    return outcome


def _navicore_library_problems(plain, asked, accepted):
    """Which library NaviCore runs, from what its two pulls ended in, and whether its ,P pull ended the way that library
    must. A library from before F13 drops packet type 18 - every part and refusal - so it is silent for both pulls. A
    newer one gives itself away by answering the plain pull (CFGERR NOPARTS), or by the parts request (type 19) W2
    accepted from it (`accepted`), which nothing older sends. Its ,P pull must then end in joinable parts, or a NOMEM or
    CHANGED that passes on its own: silence means type 18 never reached NaviCore's console, and NOPARTS - asked twice
    by then - that its parts request never reached W2."""
    proof = [why for why, yes in ((f"the plain pull was {plain}", plain != "silent"),
                                  ("W2 accepted its parts request", accepted)) if yes]
    if not proof:
        if asked == "silent":
            return []
        return [f"the plain pull was silent, as through a library from before F13, but the ,P pull ended in {asked}: "
                f"an old library is silent for both, a new one for neither"]
    new, problems = f"NaviCore's library has F13 ({'; '.join(proof)})", []
    if plain == "silent":
        problems.append(f"{new}, yet the plain pull was silent: its CFGERR NOPARTS (packet type 18) never reached "
                        f"NaviCore's console")
    last = asked.split(", then ")[-1]
    if not (last.startswith("parts") or last in NC_PARTS_PASS):
        why = {"silent": ": nothing on packet type 18 reached NaviCore's console",
               "refused NOPARTS": ": its parts request (type 19) never reached W2"}.get(last, "")
        problems.append(f"{new}, so its ,P pull must end in joinable parts or a passing NOMEM or CHANGED, not {asked}"
                        f"{why}")
    return problems


@test("navicore.pull_over_limit", "A config over 2912 characters never reaches NaviCore's WCB_Client relay as a [MGMT:CONFIG,2] line: a library from before F13 drops the target's parts and refusals and prints nothing; a newer one - known by any answer, or by the parts request W2 accepts from it - prints CFGERR NOPARTS for a plain pull and joinable parts for ,P, asked again once after a NOPARTS (F13)", needs=["navicore", "wcb2"], links=[])
def pull_over_limit_via_navicore(bench):
    """An old relay's silence only means something if the same relay can reach W2 at all, so a one-line pull through
    NaviCore comes first. Silence is also what a new library that drops packet type 18 would give, so W2 logs the
    requests it accepts (?DEBUG,MGMT: RAM only, so config_guard never sees it), and a parts request from NaviCore marks
    the library as new. The throwaway sequences on W2 are removed again."""
    nc, w2 = _nc(bench), WCB(bench.dev("wcb2"))
    keys, problems = [], []
    with config_guard(bench, 2):
        try:
            ver = w2.version()
            pull_config(nc.dev, 2, parts=False, timeout=10)      # reachable: the silence below means something
            n = _grow_over(w2, ver, keys)
            want = _factory_reply(w2, ver)
            assert _has(w2.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled"), "W2 did not turn ?DEBUG,MGMT on"
            plain = _navicore_pull_outcome(nc, False, n, want, problems)
            m = w2.dev.mark()
            asked = _navicore_pull_outcome(nc, True, n, want, problems)
            accepted = any(NC_PARTS_ACCEPTED.search(x) for x in w2.dev.since(m))
            problems += _navicore_library_problems(plain, asked, accepted)
            bench.note(f"NaviCore, W2 at {n} characters: plain pull {plain}; ,P pull {asked}; W2 "
                       f"{'accepted a' if accepted else 'logged no'} parts request from it: a library "
                       f"{'with' if plain != 'silent' or accepted else 'from before'} F13")
        finally:
            try:
                w2.run("?DEBUG,MGMT,OFF")
            finally:
                _clear(w2, keys)
    assert not problems, "; ".join(problems)


# ============================================================ TRIGGER, record/replay, #L diagnostics, a temporary probe
@test("navicore.trigger_bounds_usb_vs_mesh", "TRIGGER ranges: USB rejects a bad button or tap; the mesh clamps tap (9 -> 4, 0 -> 1) and drops a bad button", needs=["navicore", "wcb1"], links=[])
def trigger_bounds_usb_vs_mesh(bench):
    nc, w = _nc(bench), usb_wcb(bench)
    if "136" in nc.config().get("mappings", {}):
        raise Skip("slot 136 (mode 1, button 36) is mapped: its tiers would fire real actions")
    bad = []
    for btn, tap in ((37, 1), (36, 5), (36, 0)):
        m = nc.dev.mark()
        ack = nc.ack_line({"type": "TRIGGER", "mode": 1, "btn": btn, "tap": tap})
        if ack != '{"type":"ACK","ok":false,"msg":"bad mode/btn/tap"}' or _has(nc.dev.since(m), '"type":"rc_trig"'):
            bad.append(f"USB btn {btn} tap {tap}: {ack}")
    m = nc.dev.mark()
    ack = nc.ack_line({"type": "TRIGGER", "mode": 1, "btn": 36, "tap": 1})
    time.sleep(0.3)
    lines = [x.rstrip() for x in nc.dev.since(m)]
    order = [next((i for i, x in enumerate(lines) if x == want), None) for want in
             ("[TRIGGER] mode=1 btn=36 tap=1", '{"sys":1,"type":"rc_trig","id":20,"mode":1,"btn":36,"tap":1}', '{"type":"ACK","ok":true}')]
    if None in order or order != sorted(order):
        bad.append(f"valid USB trigger lines {lines}")
    for tap, clamped in ((9, 4), (0, 1)):
        m = nc.dev.mark()
        w.send(f';W20,{{"type":"TRIGGER","mode":1,"btn":36,"tap":{tap}}}')
        time.sleep(2)
        lines = [x.rstrip() for x in nc.dev.since(m)]
        if f'{{"sys":1,"type":"rc_trig","id":20,"mode":1,"btn":36,"tap":{clamped}}}' not in lines or _has(lines, "[TRIGGER]"):
            bad.append(f"mesh tap {tap}: {lines}")
    m = nc.dev.mark()
    w.send(';W20,{"type":"TRIGGER","mode":1,"btn":37,"tap":1}')
    time.sleep(2)
    if _has(nc.dev.since(m), '"type":"rc_trig"'):
        bad.append("a mesh trigger for button 37 fired")
    assert not bad, "; ".join(bad)


def _tier_actions(tier):
    if isinstance(tier, list):
        return tier
    if isinstance(tier, dict):
        return tier.get("actions", [])
    return []


@test("navicore.trigger_dispatch_trace", "A USB TRIGGER on a mapped tier prints each configured WCB action's dispatch line, in order (fires real show actions)", needs=["navicore", "wcb1"], links=[])
def trigger_dispatch_trace(bench):
    """Only a mapping whose tier 1 is nothing but wcb_unicast / wcb_broadcast actions, each delayed at most 2 s, is
    used; anything with record/play/stop, HCR, sound, WLED, serial or Maestro actions is skipped."""
    nc = _nc(bench)
    cfg = nc.config()
    choice = None
    for key, mapping in sorted(cfg.get("mappings", {}).items()):
        actions = _tier_actions(mapping.get("t1"))
        if actions and all(a.get("type") in ("wcb_unicast", "wcb_broadcast") and int(a.get("delay", a.get("delayMs", 0)) or 0) <= 2000 for a in actions):
            choice = (int(key), actions)
            break
    if not choice:
        raise Skip("no mapping whose tier 1 holds only WCB actions")
    key, actions = choice
    mode, btn = divmod(key, 100)
    expected = [f"[DISPATCH] WCB→{a.get('target')}  {a.get('cmd')}" if a["type"] == "wcb_unicast" else f"[DISPATCH] WCB broadcast  {a.get('cmd')}"
                for a in actions]
    longest = max(int(a.get("delay", a.get("delayMs", 0)) or 0) for a in actions)
    with config_guard(bench, 1, 2), nc.debug(DBG_MAESTRO | DBG_WCB):
        m = nc.dev.mark()
        ack = nc.ack_line({"type": "TRIGGER", "mode": mode, "btn": btn, "tap": 1})
        time.sleep(longest / 1000 + 2)
        lines = [x.rstrip() for x in nc.dev.since(m)]
    assert f"[TRIGGER] mode={mode} btn={btn} tap=1" in lines, "no [TRIGGER] line"
    positions = [next((i for i, x in enumerate(lines) if x == e), None) for e in expected]
    assert None not in positions and positions == sorted(positions), f"expected {expected} in order, NaviCore printed {lines}"
    assert ack == '{"type":"ACK","ok":true}', ack


@test("navicore.rec_info_list", "Record/replay observability: ?REC,INFO and ?REC,LS markers, and PLAY of a missing clip, with no flash writes", needs=["navicore"], links=[])
def rec_info_list(bench):
    """Read-only. Never ?REC,START/SAVE/RM/RENAME/EDIT*/CLEAR/LOAD: they write flash or replace the RAM take."""
    nc = _nc(bench)
    before = nc.rec_info()
    if before[0] != "idle":
        raise Skip(f"NaviCore's recorder is {before[0]}")
    lines, items = nc.clips()
    missing = f"HILnope{nonce()}"
    m = nc.dev.mark()
    nc.dev.send(f"?REC,PLAY,{missing}")
    nc.dev.expect(rf"^\[REC\] clip '{missing}' not found", timeout=3, since=m)
    after = nc.rec_info()
    assert "[REC] clips:" in [x.rstrip() for x in lines] and "[CLIPLIST:BEGIN]" in [x.rstrip() for x in lines], lines[:4]
    assert all({"name", "bytes", "dur", "n"} <= set(i) for i in items), items
    assert after == before, f"INFO changed: {before} -> {after}"


@test("navicore.rec_play_clip", "OPT-IN (navicore_clip, attended): play a saved clip of up to 30 s; the start line, REPLAYING state and completion timing", needs=["navicore", "wcb1"], links=[], opt_in="navicore_clip")
def rec_play_clip(bench):
    """Drives every Maestro channel in the clip and re-fires its recorded actions, possibly WCB commands (hence
    config_guard). The clip must hold no record/play/stop action: a recorded record would start a take that saves.
    The buffer keeps the last clip played or loaded until a take, a CLEAR or a reboot, so a buffer whose event count
    and duration match a saved clip's is that copy, not a take. Until 2026-09-28 the test left its clip there: run
    20260927-174702 played 8-2 (191 events, 1767 ms) and every later run skipped. It now puts the buffer back as found,
    empty (?REC,CLEAR) or that clip (?REC,LOAD): both RAM only (navicore_record.h clearClip, loadClip)."""
    nc = _nc(bench)
    state = nc.rec_info()
    clips = nc.clips()[1]
    copy_of = next((c["name"] for c in clips if (str(c["n"]), str(c["dur"])) == (state[1], state[3])), None)
    if state[0] != "idle" or (state[1] != "0" and not copy_of):
        raise Skip("the recorder is busy or holds an unsaved take")
    clip = next((c for c in clips if c["dur"] <= 30000), None)
    if not clip:
        raise Skip("no saved clip of 30 s or less")
    with config_guard(bench, 1, 2):
        try:
            m = nc.dev.mark()
            nc.dev.send(f"?REC,PLAY,{clip['name']}")
            g = nc.dev.expect(r"^\[REC\] replaying (\d+) events over (\d+)ms — \?REC,STOP to abort", timeout=3, since=m)
            events, dur = int(g.group(1)), int(g.group(2))
            started = next(ts for ts, x in list(nc.dev.lines[m:]) if x.startswith("[REC] replaying"))
            time.sleep(0.5)
            during = nc.rec_info()
            nc.dev.expect(r"^\[REC\] ▶ playback complete", timeout=dur / 1000 + 5, since=m)
            took = (next(ts for ts, x in list(nc.dev.lines[m:]) if "playback complete" in x) - started) * 1000
            after = nc.rec_info()
        finally:
            if nc.rec_info()[0] == "REPLAYING":
                nc.dev.send("?REC,STOP")
                time.sleep(0.5)
            rm = nc.dev.mark()
            nc.dev.send(f"?REC,LOAD,{copy_of}" if state[1] != "0" else "?REC,CLEAR")
            nc.dev.expect(r"^\[REC\] (loaded|cleared)", timeout=3, since=rm)
            back = nc.rec_info()
    bench.note(f"clip {clip['name']}: {events} events over {dur} ms, completed after {took:.0f} ms; the buffer was "
               f"{'a copy of ' + copy_of if state[1] != '0' else 'empty'} and is again")
    assert events == clip["n"], f"replaying {events} events, the clip lists {clip['n']}"
    assert during[0] == "REPLAYING", during
    assert dur - 50 <= took <= dur + 1500, f"completed after {took:.0f} ms for a {dur} ms clip"
    assert after[0] == "idle", after
    assert (back[1], back[3]) == (state[1], state[3]), f"the recorder buffer was not left as found: {state} -> {back}"


@test("navicore.cli_codes", "#L diagnostics: an unknown code, lowercase, #L1, the #L11 board list against GET_WCB_STATUS, the #L13 raw frame", needs=["navicore"], links=[])
def cli_codes(bench):
    """Never #L2/#L02 (restart) or #L20/#L21 (HCR test frames); #L10 is navicore.sbus_live_dump_toggle."""
    nc = _nc(bench)
    st = nc.wcb_status()
    known = [i + 1 for i, v in enumerate(st.get("known", [])) if v]
    online, clients = st.get("online", []), st.get("clients", [0] * 20)
    bad = []
    if "Unknown #L code 99. Valid: 1,2,9,10,11,12,13,20,21" not in nc.cli("#L99", r"Unknown #L code 99", flush=False):
        bad.append("#L99")
    if not any(re.match(r"^Mode=\d+  matrixBtn=\S+  matrixVal=\S+$", x) for x in nc.cli("#l12", r"Mode=\d+", flush=False)):
        bad.append("#l12")
    # #L1 names the booted profile (navicore.l1_names_board_profile), so either name is correct here.
    if not any(x in ("NaviCore — WCB HW 3.2", "NaviCore — NaviCore v2") for x in nc.cli("#L1", r"NaviCore — ", flush=False)):
        bad.append("#L1")
    l11 = nc.cli("#L11", r"Board status: ", flush=False)
    if f"WCB device ID: 20  quantity: {st.get('quantity')}" not in l11:
        bad.append(f"#L11 header {l11[:2]}")
    status = next((x for x in l11 if x.startswith("  Board status: ")), "")
    boards = {int(i): up for i, up, _ in re.findall(r"WCB(\d+)=(UP|dn)(\*?)", status)}
    if set(boards) != set(known):
        bad.append(f"#L11 lists {sorted(boards)}, GET_WCB_STATUS knows {known}")
    bad += [f"#L11 WCB{i}={up} vs online {online[i - 1]}" for i, up in boards.items()
            if i <= len(online) and not clients[i - 1] and (up == "UP") != bool(online[i - 1])]   # arrays end at the highest known id
    raw = nc.cli("#L13", r"^---- SBUS RAW ---- \(\d+ bytes, SBUS-\d+\)", flush=False)
    if "---- SBUS RAW ---- (36 bytes, SBUS-24)" in raw:
        for want in ("  bytes 23-33  = CH17-24 data  ← check these", "  byte 34      = flags", "  byte 35      = footer (expect 00)"):
            if want not in raw:
                bad.append(f"#L13 lacks {want!r}")
        if not any(x.startswith("  [ 0] 0F ") for x in raw):
            bad.append("#L13 first row does not start with the 0F header")
    assert not bad, "; ".join(bad)


@test("navicore.l1_names_board_profile", "(should) #L1 names the configured board profile, not always 'WCB HW 3.2'", needs=["navicore"], links=[])
def l1_names_board_profile(bench):
    """#L01 prints 'NaviCore — WCB HW 3.2' whatever rcConfig.boardType is (NaviCore.ino:3599 vs applyBoardProfile
    3180-3198), so on a NaviCore v2 profile the diagnostic names the wrong hardware."""
    nc = _nc(bench)
    if nc.config().get("boardType") == 1:
        raise Skip("the board profile is WCB HW 3.2, where the line is right")
    line = next((x for x in nc.cli("#L1", r"NaviCore — ", flush=False) if x.startswith("NaviCore — ")), "")
    assert "WCB HW 3.2" not in line, f"#L1 on a boardType-0 profile printed {line!r}"


@test("navicore.sbus_live_dump_toggle", "#L10 starts and stops the 1 Hz SBUS state dump", needs=["navicore"], links=[])
def sbus_live_dump_toggle(bench):
    nc = _nc(bench)
    on = False
    try:
        m = nc.dev.mark()
        nc.dev.send("#L10")
        on = nc.dev.expect(r"^SBUS live dump (ON \(1Hz\)|OFF)", timeout=3, since=m).group(1) != "OFF"
        if not on:                                    # it was already running: this toggle stopped it
            m = nc.dev.mark()
            nc.dev.send("#L10")
            nc.dev.expect(r"^SBUS live dump ON \(1Hz\)", timeout=3, since=m)
            on = True
        time.sleep(3.5)
        blocks = sum(x.startswith("---- SBUS STATE ----") for x in nc.dev.since(m))
        m2 = nc.dev.mark()
        nc.dev.send("#L10")
        nc.dev.expect(r"^SBUS live dump OFF", timeout=3, since=m2)
        on = False
        time.sleep(2)
        tail = nc.dev.since(m2)
        off_at = next(i for i, x in enumerate(tail) if x.startswith("SBUS live dump OFF"))
        after = sum(x.startswith("---- SBUS STATE ----") for x in tail[off_at + 1:])
    finally:
        if on:
            nc.dev.send("#L10")
            time.sleep(0.5)
    assert 2 <= blocks <= 4, f"{blocks} state blocks in 3.5 s at 1 Hz"
    assert after == 0, f"{after} state blocks after OFF"


@test("navicore.probe_temp_peer_not_learned", "A temporary client (probe1) shows on NaviCore as a live client neighbour but is never joined or persisted, and ages out (slow, up to ~4 min)", needs=["navicore", "wcb1", "probe1"], links=[])
def probe_temp_peer_not_learned(bench):
    """Never an id NaviCore has learned: a temporary advert from a learned id makes WCB_Client forget it, an NVS
    write (WCB_Client.cpp:238-246). Auto-join is gated on !temporary (WCB_Client.cpp:2125-2132); the neighbour row
    ages out after 180 s (WCB_Client.h:108). The WCBs forget the probe when it leaves; NaviCore is left to age it out."""
    nc = _nc(bench)
    known = nc.wcb_status().get("known", [])
    # known[] runs only up to the highest id NaviCore knows (NaviCore.ino WCB_STATUS), so an id past its end is unknown.
    pid = next((p for p in (16, 15, 13, 12, 11, 10) if not (p <= len(known) and known[p - 1])), None)
    if pid is None:
        raise Skip("no candidate probe id is unknown to NaviCore")

    def peers_count():
        peers = nc.wdpcfg().get("PEERS", "")
        return int(peers) if peers.isdigit() else None

    peers0 = peers_count()
    nm = nc.dev.mark()
    with probe_in_mesh(bench, "probe1", pid):
        time.sleep(10)
        status = nc.wcb_status()
        row = {int(r["N"]): r for r in nc.wdp_dump()}.get(pid)
    gone, deadline = False, time.monotonic() + 240
    while time.monotonic() < deadline:
        time.sleep(20)
        now_known = nc.wcb_status().get("known", [])
        if not (pid <= len(now_known) and now_known[pid - 1]):   # the array shrinks once the probe ages out
            gone = True
            break
    lines = nc.dev.since(nm)
    rows_after = {int(r["N"]) for r in nc.wdp_dump()}
    assert row and row.get("CLIENT") == "1" and row.get("ALIAS") == "HILProbe" and row.get("FW") == "wcb_probe-2" and row.get("PEER") == "0", f"NaviCore row {row}"
    for key in ("known", "clients", "temporary", "online"):
        assert status.get(key, [0] * 20)[pid - 1] == 1, f"GET_WCB_STATUS {key}[{pid - 1}] = {status.get(key, [None] * 20)[pid - 1]}"
    assert not _has(lines, f"[WCB_Client] auto-joined WCB{pid}") and not _has(lines, f"[PEER] New WCB {pid}"), "NaviCore joined the temporary probe"
    assert gone and pid not in rows_after, "NaviCore did not age the probe out within 240 s"
    assert peers_count() == peers0, "NaviCore's live peer count changed"


# ============================================================ the SBUS controller
def _sbus_setup(bench):
    """(SbusCtl, NaviCore, the controller's getcfg, NaviCore's GET_CONFIG), or Skip unless NaviCore sees a full-rate
    SBUS-24 stream."""
    ctl, nc = SbusCtl(bench.dev("sbus")), _nc(bench)
    state = nc.sbus_dump()
    if state["fps"] < SBUS_FULL_FPS or state["variant"] != "SBUS-24":
        raise Skip(f"NaviCore sees no full-rate SBUS-24 stream (fps {state['fps']}, {state['variant']})")
    return ctl, nc, ctl.cfg(), nc.config()


@test("sbus.discover", "SBUS controller config and NaviCore bindings read back; the safe channels are computed; NaviCore sees SBUS-24 at full rate", needs=["sbus", "navicore"], links=[])
def discover(bench):
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    missing = [k for k in ("sbus24", "aMin", "aMax", "aRev", "sw", "sl", "tr", "btn", "lua") if k not in cfg]
    safe = safe_channels(ncfg)
    bench.note(f"SBUS channels NaviCore binds nothing to: {sorted(safe)}")
    assert not missing, f"controller getcfg lacks {missing}"
    assert "matrixChannel" in ncfg and "mappings" in ncfg, "NaviCore GET_CONFIG lacks matrixChannel or mappings"


@test("sbus.btn_single_tap", "A controller matrix button: NaviCore decodes the slot and emits exactly one tap-1 rc_trig tapWindowMs after release (whether W1 relayed it is noted, not asserted: a best-effort broadcast)", needs=["sbus", "navicore", "wcb1"], links=[])
def btn_single_tap(bench):
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    w = usb_wcb(bench)
    mode = nc.mode()
    i, button, slot = matrix_button(nc, cfg, ncfg, mode)
    tap_ms = ncfg.get("tapWindowMs", 500)
    rc = f'{{"sys":1,"type":"rc_trig","id":20,"mode":{mode},"btn":{slot},"tap":1}}'
    w.send(';W20,{"type":"PING"}')                      # opens W1's 20 s relay window
    time.sleep(1.0)
    try:
        nm, wm = nc.dev.mark(), w.dev.mark()
        ctl.button(i, True)
        pressed = time.monotonic()
        time.sleep(0.15)
        held = nc.cli("#L12", r"Mode=\d+", flush=False)
        time.sleep(max(0.0, pressed + 0.2 - time.monotonic()))
        ctl.button(i, False)
        released = time.monotonic()
        time.sleep(tap_ms / 1000 + 1.5)
        trigs = nc.rc_events(nm, btn=slot)
        relayed = _has(w.dev.since(wm), rc)
        after = nc.sbus_dump()["channels"][ncfg["matrixChannel"] - 1]
    finally:
        ctl.button(i, False)
    bench.note(f"rc_trig relayed to W1: {relayed} (best-effort broadcast)")
    assert f"Mode={mode}  matrixBtn={slot}  matrixVal={button['v']}" in held, f"while held NaviCore said {held}"
    assert len(trigs) == 1 and trigs[0][1]["tap"] == 1, f"rc_trig lines {trigs}"
    late = (trigs[0][0] - released) * 1000
    assert tap_ms - 50 <= late <= tap_ms + 300, f"rc_trig {late:.0f} ms after release (tapWindowMs {tap_ms})"
    assert after == SBUS_CENTER, f"the matrix channel reads {after} after release"


@test("sbus.btn_double_triple_tap", "Two and three quick presses give one rc_trig with tap 2 / tap 3, timed from the last press", needs=["sbus", "navicore"], links=[])
def btn_double_triple_tap(bench):
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    i, _, slot = matrix_button(nc, cfg, ncfg, mode)
    tap_ms = ncfg.get("tapWindowMs", 500)
    bad = []
    try:
        for presses in (2, 3):
            nm = nc.dev.mark()
            for _ in range(presses):
                ctl.button(i, True)
                last = time.monotonic()
                time.sleep(0.1)
                ctl.button(i, False)
                time.sleep(0.12)
            time.sleep(tap_ms / 1000 + 2)
            trigs = nc.rc_events(nm, btn=slot)
            if [t["tap"] for _, t in trigs] != [presses]:
                bad.append(f"{presses} presses gave taps {[t['tap'] for _, t in trigs]}")
            elif not tap_ms - 50 <= (trigs[0][0] - last) * 1000 <= tap_ms + 300:
                bad.append(f"{presses} presses: rc_trig {(trigs[0][0] - last) * 1000:.0f} ms after the last press")
    finally:
        ctl.button(i, False)
    assert not bad, "; ".join(bad)


@test("sbus.btn_hold_unconfigured_long", "Holding a button whose slot has no tier-4 mapping still gives tap 1 on release, no long press", needs=["sbus", "navicore"], links=[])
def btn_hold_unconfigured_long(bench):
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    mode = nc.mode()
    i, _, slot = matrix_button(nc, cfg, ncfg, mode)
    tap_ms, hold_ms = ncfg.get("tapWindowMs", 500), ncfg.get("holdMs", 750)
    try:
        nm = nc.dev.mark()
        ctl.button(i, True)
        time.sleep((hold_ms + 500) / 1000)
        while_held = nc.rc_events(nm, btn=slot)
        ctl.button(i, False)
        released = time.monotonic()
        time.sleep(tap_ms / 1000 + 1.5)
        trigs = nc.rc_events(nm, btn=slot)
    finally:
        ctl.button(i, False)
    assert not while_held, f"rc_trig while held: {while_held}"
    assert [t["tap"] for _, t in trigs] == [1], f"taps {[t['tap'] for _, t in trigs]}"
    assert tap_ms - 50 <= (trigs[0][0] - released) * 1000 <= tap_ms + 300, "tap 1 not timed from the release"


@test("sbus.mode_sets_trigger_mode", "After a mesh SET_MODE, a controller matrix press dispatches under the new mode", needs=["sbus", "navicore", "wcb1"], links=[])
def mode_sets_trigger_mode(bench):
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    w = usb_wcb(bench)
    m0 = nc.mode()
    i, _, slot = matrix_button(nc, cfg, ncfg, m0)
    m1 = next((m for m in (1, 2, 3) if m != m0 and str(m * 100 + slot) not in ncfg.get("mappings", {})), None)
    if m1 is None:
        raise Skip("the chosen slot is mapped in every other mode")
    tap_ms = ncfg.get("tapWindowMs", 500)
    try:
        w.send(f';W20,{{"type":"SET_MODE","mode":{m1}}}')
        time.sleep(2)
        if nc.mode() != m1:
            raise AssertionError("the mesh SET_MODE did not take")
        nm = nc.dev.mark()
        ctl.button(i, True)
        time.sleep(0.2)
        ctl.button(i, False)
        time.sleep(tap_ms / 1000 + 1.5)
        trigs = nc.rc_events(nm, btn=slot)
    finally:
        ctl.button(i, False)
        w.send(f';W20,{{"type":"SET_MODE","mode":{m0}}}')
        time.sleep(2)
    assert [(t["mode"], t["tap"]) for _, t in trigs] == [(m1, 1)], f"rc_trig lines {trigs}"
    assert nc.mode() == m0, "the original mode was not restored"


@test("sbus.switch_exact", "A controller switch position arrives at NaviCore as its exact configured value; a 2-position switch's middle snaps low", needs=["sbus", "navicore"], links=[])
def switch_exact(bench):
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    safe = safe_channels(ncfg)
    k, sw = next(((k, s) for k, s in enumerate(cfg.get("sw", [])) if s.get("c") in safe), (None, None))
    if sw is None:
        raise Skip("no controller switch on a channel NaviCore leaves unbound")
    c, v, p0 = sw["c"], sw["v"], sw.get("pos", 0)
    base = nc.sbus_dump()["channels"]
    got = {}
    try:
        for p in (0, 1, 2):
            ctl.switch(k, p)
            time.sleep(0.4)
            got[p] = nc.sbus_dump()["channels"]
    finally:
        ctl.switch(k, p0)
        time.sleep(0.4)
    want = {0: v[0], 1: v[1], 2: v[2]} if sw.get("t", 0) == 0 else {0: v[0], 1: v[0], 2: v[2]}
    for p in (0, 1, 2):
        assert got[p][c - 1] == want[p], f"position {p}: CH{c} = {got[p][c - 1]}, expected {want[p]}"
        moved = [n + 1 for n in range(24) if n != c - 1 and got[p][n] != base[n]]
        assert not moved, f"position {p} also moved CH{moved}"
    assert nc.sbus_dump()["channels"][c - 1] == base[c - 1], "the switch channel did not return to its value"


@test("sbus.slider_exact", "A controller slider's percent arrives at NaviCore as its exact SBUS value (0/25/50/75/100 %, out-of-range clamped)", needs=["sbus", "navicore"], links=[])
def slider_exact(bench):
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    safe = safe_channels(ncfg)
    j, sl = next(((j, s) for j, s in enumerate(cfg.get("sl", [])) if s.get("c") in safe), (None, None))
    if sl is None:
        raise Skip("no controller slider on a channel NaviCore leaves unbound")
    c, pct0 = sl["c"], sl.get("pct", 50)
    want = {0: 172, 25: 582, 50: 992, 75: 1401, 100: 1811, 150: 1811, -5: 172}     # (uint16)(pct/100*1639 + 172 + 0.5)
    bad = []
    try:
        for pct, value in want.items():
            ctl.slider(j, pct)
            time.sleep(0.4)
            got = nc.sbus_dump()["channels"][c - 1]
            if got != value:
                bad.append(f"{pct} %: CH{c} = {got}, expected {value}")
    finally:
        ctl.slider(j, pct0)
        time.sleep(0.4)
    assert not bad, "; ".join(bad)


@test("sbus.trim_exact", "A controller trim gives exact channel values at NaviCore: button mode on the matrix channel, under a mode where both of its slots are unmapped; step mode only when a step trim sits on an unbound channel (unreachable on this bench without a flash write)", needs=["sbus", "navicore", "wcb1"], links=[])
def trim_exact(bench):
    """A trim on a channel NaviCore leaves unbound is used first. This bench has none: all six trims are button-mode
    trims on the matrix channel (the X18 layout wires T1-T6 into the button matrix), and only the controller's 'cfg'
    moves a trim's channel or mode, which saves to flash (SBUSController.ino:1285-1289, :1343), so the step-mode branch
    never runs here. The fallback presses a matrix trim only under a mode where neither of its slots has a mapping key,
    so nothing can run (rc_config.h:1255-1262; rcDispatch then only emits rc_trig, NaviCore.ino:2213-2285), switching
    to that mode with a mesh SET_MODE from W1 as sbus.mode_sets_trigger_mode does. SET_MODE saves nothing, but it is not
    free of side effects: every mode-aware knob re-dispatches, so J2 moves the real servo on NaviCore's Maestro slot 1
    ch 0, and that channel's 1500 ms auto-release stays armed in RAM until reboot (why
    navicore.maestro_mesh_settarget_readback avoids knob-driven channels). Pressing the other slot commits the first
    one's tap (NaviCore.ino:2302-2308), so the rc_trig lines must be exactly one tap 1 per slot."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    safe = safe_channels(ncfg)
    t, tr = next(((t, x) for t, x in enumerate(cfg.get("tr", [])) if x.get("c") in safe), (None, None))
    mc, maps = ncfg.get("matrixChannel"), ncfg.get("mappings", {})
    m0 = nc.mode()
    m1, sR, sL = m0, None, None
    if tr is None:
        if not mc:
            raise Skip("NaviCore's GET_CONFIG names no matrix channel")
        if band(ncfg, nc.sbus_dump()["channels"][mc - 1]) or band(ncfg, SBUS_CENTER):
            raise Skip("the matrix channel's resting or release value already decodes to a slot")
        for m in [m0] + [x for x in (1, 2, 3) if x != m0]:
            for i, x in enumerate(cfg.get("tr", [])):
                if x.get("c") != mc or x.get("m", 0) != 1:
                    continue
                # Both directions must decode, to two different slots: a None slot or a repeat has no exact rc_trig list.
                a, b = band(ncfg, x.get("vR", 0)), band(ncfg, x.get("vL", 0))
                if a and b and a != b and str(m * 100 + a) not in maps and str(m * 100 + b) not in maps:
                    t, tr, m1, sR, sL = i, x, m, a, b
                    break
            if tr is not None:
                break
        if tr is None:
            raise Skip("no trim on an unbound channel, and no matrix trim whose two slots are unmapped in any mode")
    c, mode, step, cur0 = tr["c"], tr.get("m", 0), tr.get("s", 0), tr.get("cur", SBUS_CENTER)
    w = usb_wcb(bench)
    got = trigs = None

    def read(d, pressed=None):
        ctl.trim(t, d, pressed)
        time.sleep(0.4)
        return nc.sbus_dump()["channels"][c - 1]

    try:
        if m1 != m0:
            w.send(f';W20,{{"type":"SET_MODE","mode":{m1}}}')
            time.sleep(2)
            if nc.mode() != m1:
                raise AssertionError("the mesh SET_MODE did not take")
        nm = nc.dev.mark()
        if mode == 0:
            up, down = read(1), read(-1)
            assert up == min(cur0 + step, SBUS_MAX), f"step up: CH{c} = {up}"
            if cur0 + step <= SBUS_MAX:
                assert down == cur0, f"step back: CH{c} = {down}, expected {cur0}"
        else:
            got = [read(1, True), read(1, False), read(-1, True), read(-1, False)]
            if sR:
                time.sleep(ncfg.get("tapWindowMs", 500) / 1000 + 1.0)    # the last tap fires tapWindowMs after release
                trigs = [(x["mode"], x["btn"], x["tap"]) for _, x in nc.rc_events(nm)]   # every slot, not just these
    finally:
        if mode == 1:
            ctl.trim(t, 1, False)    # a release centres the trim either way
        if m1 != m0:
            w.send(f';W20,{{"type":"SET_MODE","mode":{m0}}}')
            time.sleep(2)
    assert nc.mode() == m0, "the original mode was not restored"
    if mode == 1:
        assert got == [tr.get("vR"), SBUS_CENTER, tr.get("vL"), SBUS_CENTER], f"button-mode trim values {got}"
    if sR:
        assert trigs == [(m1, sR, 1), (m1, sL, 1)], f"rc_trig (mode, btn, tap) {trigs}, expected slots {sR} then {sL} in mode {m1}"


@test("sbus.signal_loss_controller_reset", "OPT-IN (sbus_reset): resetting the controller stops SBUS frames; NaviCore's frame counter stops for over a second with ageMs rising and fps falling, flags unchanged and no dispatch, then recovers", needs=["sbus", "navicore"], links=[], opt_in="sbus_reset")
def signal_loss_controller_reset(bench):
    """The reset is RTS=1/DTR=0 on the controller's USB-Serial/JTAG port, which resets the chip (SbusCtl.reset_rts,
    serialdev.usb_jtag_reset: DTR re-written after every RTS change, since Windows' usbser.sys sends the line state
    only on a DTR write; DTR stays 0, so the chip boots the app, not download mode). The controller's boot record
    proves the reset happened before NaviCore is blamed. Its boot resets its runtime state, so the test only runs when
    that state already equals the boot defaults."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    if (any(s.get("pos") != s.get("d") for s in cfg.get("sw", [])) or any(s.get("pct") != 50 for s in cfg.get("sl", []))
            or any(x.get("cur") != SBUS_CENTER for x in cfg.get("tr", []))):
        raise Skip("the controller is not in its boot state; a reset would move channels")
    base = nc.sbus_dump()
    b0 = ctl.bootlog()
    nm = nc.dev.mark()
    t_pulse = time.monotonic()
    ctl.reset_rts()             # the port may go away with the reset; it is reopened below
    # Poll through the outage. While no frame arrives NaviCore's frame counter stays put and its ageMs must rise. The
    # outage is the longest run of polls on one frame count, not "the poll where fps reads 0": fps is a one-second
    # average, so it can still read 6 on the last frozen poll and reach 0 only once frames are back (run
    # 20260924-112929: frames 1030039 at ageMs 204, 856, 1507, then fps=0 with the counter moving again). And a later
    # poll proves nothing - the controller can be back within a second (run 20260924-092602). fps reaching 0 is
    # checked on its own.
    samples, deadline = [], time.monotonic() + 4
    while time.monotonic() < deadline:
        samples.append(nc.sbus_dump())
        time.sleep(0.4)
    runs = []
    for x in samples:
        if runs and x["frames"] == runs[-1][-1]["frames"]:
            runs[-1].append(x)
        else:
            runs.append([x])
    frozen = max(runs, key=len) if runs else []
    time.sleep(1.0)
    outage_trigs = [x for x in nc.dev.since(nm) if '"type":"rc_trig"' in x]
    time.sleep(2)
    bench.close_device("sbus")
    reopened, deadline = None, time.monotonic() + 60
    while reopened is None and time.monotonic() < deadline:
        try:
            reopened = bench.dev("sbus")
        except Exception:
            time.sleep(2)
    assert reopened is not None, "the SBUS controller's port did not come back within 60 s"
    boot = reopened.since(0)
    ctl = SbusCtl(reopened)
    ctl.ping()
    b1 = ctl.bootlog(tries=2)
    time.sleep(3)
    recovered = nc.sbus_dump()
    assert not _has(boot, "WiFi section missing from config — upgrading file."), "the controller rewrote its config on boot: report it"
    # Not the reset reason: which RTC code this reset reports is unverified here, and n counts it whatever it is.
    assert b1["n"] > b0["n"] or b1["up"] < (time.monotonic() - t_pulse) * 1000, (
        f"the controller did not reset: boot count {b0['n']}->{b1['n']}, up {b1['up']} ms ({b1.get('rstn')}, {b1.get('rtcn')})")
    # fps is not required to read exactly 0: a stray frame as the controller resets leaves it at 1 for the whole outage
    # (run 20260924-113016: frames froze at ageMs 54, 606, 1158 while fps read 46, 46, 1, 1).
    ages = [x["age"] for x in frozen]
    assert len(ages) >= 2 and all(b > a for a, b in zip(ages, ages[1:])) and ages[-1] >= 1000, (
        f"NaviCore's frame counter was not frozen for a second with ageMs rising: {ages}")
    low = min(x["fps"] for x in samples)
    assert low <= base["fps"] // 2, f"NaviCore's fps never fell during the outage (lowest {low}, normally {base['fps']})"
    assert all("lost=no" in x["text"] and "failsafe=no" in x["text"] for x in frozen), (
        "the frame flags changed without a decoded frame")
    assert not outage_trigs, f"dispatch during the outage: {outage_trigs}"
    assert recovered["fps"] >= SBUS_FULL_FPS and recovered["variant"] == base["variant"], f"after the reset: fps {recovered['fps']}, {recovered['variant']}"
