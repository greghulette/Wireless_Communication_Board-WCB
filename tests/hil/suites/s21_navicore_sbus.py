"""NaviCore beyond the basics (Maestro over the mesh, serial routing, WCB_SEND, bridged JSON, TEST_ACTION, TRIGGER,
record/replay observability, #L diagnostics, a temporary probe) and the SBUS controller's exact channel values.

Built from the verified navicore_sbus specs. Rules from the specs:
- Never change NaviCore's Maestro slots or device ids, FORGET a learned peer, record, or save config: all write
  NaviCore flash. Never #L2 (restart) or #L20/#L21 (HCR frames). The SBUS controller's serial lines always start with
  '{' ('m' and 'w' outside JSON save to flash); its mode/cfg/wificfg commands also save and are never sent.
- GET_CONFIG prints the mesh password and getcfg the WiFi credentials: they reach session.log only (gitignored).
- NaviCore's ?MAE markers print without a line end, so each command is followed by a harmless #L12 to flush it.
- Any ;W20,{json} from W1 opens a 20 s relay window: W1 USB then carries '"sys":1' lines; checks match by substring.
- SBUS channels are moved only when NaviCore's config binds nothing to them (_safe_channels); a bound channel can fire
  real actions. The one exception is the matrix channel, pressed only onto a slot with no mapping in the active mode
  (_matrix_button, sbus.trim_exact), which emits rc_trig and runs nothing.
"""
import json
import re
import time
from contextlib import contextmanager

import serial

from hil.navicore import NaviCore
from hil.runner import Skip, test
from suites.common import (Console, Watch, config_guard, link, marker, nonce, padded, probe_in_mesh, remote_wcbs,
                           require_tokens, snapshot, usb_wcb)

DBG_MAESTRO, DBG_WCB, DBG_SERIAL = 0x01, 0x02, 0x20        # NaviCore.ino:494-500


def _has(lines, text):
    return any(text in x for x in lines)


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _config(nc):
    """GET_CONFIG's data object (one line of tens of KB; it carries the mesh password)."""
    got = nc.json_cmd({"type": "GET_CONFIG"}, r'^\{"type":"CONFIG","data":', timeout=10)
    return json.loads(got.string)["data"]


def _send_json(dev, obj):
    dev.send(json.dumps(obj, separators=(",", ":")))


@contextmanager
def _debug(nc, flags):
    nc.set_debug_flags(flags)
    try:
        yield
    finally:
        nc.set_debug_flags(0)


def _flushed(nc, cmd, timeout=3.0):
    """Lines printed by a NaviCore CLI command whose reply has no line end: #L12 follows it as the flush."""
    m = nc.dev.mark()
    nc.dev.send(cmd)
    nc.dev.send("#L12")
    nc.dev.expect(r"Mode=\d+", timeout=timeout, since=m)
    return nc.dev.since(m)


def _mae_get(nc, slot, ch):
    """?MAE,GET,<slot>,<ch> -> int position, or the error word ('timeout', 'disabled')."""
    text = "\n".join(_flushed(nc, f"?MAE,GET,{slot},{ch}"))
    m = re.search(rf'\[MAE:{slot}\]\{{"q":"pos","ch":{ch},(?:"val":(\d+)|"err":"(\w+)")\}}', text)
    if not m:
        raise AssertionError(f"?MAE,GET,{slot},{ch} printed {text!r}")
    return int(m.group(1)) if m.group(1) else m.group(2)


def _local_slots(cfg):
    """[(slot, device)] for NaviCore's type-1 (local) Maestro slots."""
    return [(i + 1, m.get("device")) for i, m in enumerate(cfg.get("maestros", [])) if m.get("type") == 1]


def _usable_slot(nc, cfg, channel=0):
    """(slot, device, position) of the first local Maestro that answers, or Skip."""
    for slot, dev in _local_slots(cfg):
        pos = _mae_get(nc, slot, channel)
        if isinstance(pos, int):
            return slot, dev, pos
    raise Skip("NaviCore has no local Maestro slot that answers ?MAE,GET")


def _undriven_channel(nc, cfg):
    """(slot, device, channel, position) on a local Maestro slot for a channel NO Maestro-passthrough knob output drives,
    or Skip. A passthrough output re-dispatches on every global mode change, and one with releaseIdleMs > 0 then arms
    NaviCore's idle auto-release for that channel (NaviCore.ino processKnobs / maestroIdleReleaseTick): any later mesh
    setTarget there goes limp releaseIdleMs after it, and the arm lasts in RAM until reboot or SET_CONFIG. On this bench
    J2 drives slot 1 ch 0 with a 1500 ms release, and a SET_MODE from sbus.trim_exact in an earlier run armed it
    (full run 20260922-215719). A driven channel can also be moved by the stick at any time."""
    for slot, dev in _local_slots(cfg):
        driven = {o.get("maestroCh") for k in _entries(cfg.get("knobs")) if k.get("function") == 1
                  for key in ("outputs", "outputs2", "outputs3") for o in (k.get(key) or []) if o.get("target") == slot}
        for ch in sorted(set(range(0, 24)) - driven):
            pos = _mae_get(nc, slot, ch)   # not an int: timeout, or a channel not in servo mode
            if isinstance(pos, int):
                return slot, dev, ch, pos
            break                          # the slot does not answer - try the next one
    raise Skip("NaviCore has no local Maestro channel, free of passthrough knobs, that answers ?MAE,GET")


def _nc_lines(nc, since, pattern):
    rx = re.compile(pattern)
    return [x for x in nc.dev.since(since) if rx.search(x)]


# ============================================================ Maestro over the mesh
@test("navicore.maestro_inventory", "NaviCore's hosted Maestro devices match W1's WDP row for WCB20 and W1's WCB20 proxies (read-only)", needs=["navicore", "wcb1"], links=[])
def maestro_inventory(bench):
    """NaviCore's own ?WDP,DUMP self row hard-codes MAESTRO=- (WCB_Mgmt.h:205-208), so the hosted set comes from
    GET_CONFIG. A W1 proxy for an id NaviCore does not host is stale and never evicted (CLAUDE.md rule 5): noted, not
    failed. Proxies are auto-added only for ids 1-8 (WCB_Maestro.cpp:735)."""
    nc, w = _nc(bench), usb_wcb(bench)
    local = {dev for _, dev in _local_slots(_config(nc))}
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
    cfg = _config(nc)
    text = "\n".join(_flushed(nc, "?MAE,GET,9,0") + _flushed(nc, "?MAE,MOVING,0"))
    assert '[MAE:9]{"q":"pos","ch":0,"err":"disabled"}' in text and '[MAE:0]{"q":"mov","err":"disabled"}' in text, text
    disabled = next((i + 1 for i, m in enumerate(cfg.get("maestros", [])) if m.get("type") == 0), None)
    if disabled:
        text = "\n".join(_flushed(nc, f"?mae,get,{disabled},0"))
        assert f'[MAE:{disabled}]{{"q":"pos","ch":0,"err":"disabled"}}' in text, f"lowercase query on disabled slot {disabled}: {text!r}"
    for slot, _ in _local_slots(cfg):
        text = "\n".join(_flushed(nc, f"?MAE,MOVING,{slot}"))
        assert re.search(rf'\[MAE:{slot}\]\{{"q":"mov",("val":[01]|"err":"timeout")\}}', text), f"slot {slot}: {text!r}"


@test("navicore.maestro_mesh_settarget_readback", ";W20,;M<D>,setTarget from W1 moves NaviCore's own Maestro, not W1's; read back with ?MAE,GET", needs=["navicore", "wcb1"])
def maestro_mesh_settarget_readback(bench):
    w1s1 = link(bench, 1, "S1")
    nc, w = _nc(bench), usb_wcb(bench)
    slot, dev, ch, p0 = _undriven_channel(nc, _config(nc))
    target = 6400 if abs(p0 - 6400) > 200 else 5600
    bad = []
    with _debug(nc, DBG_MAESTRO):
        try:
            nm, pm, wm = nc.dev.mark(), w1s1.mark(), w.dev.mark()
            w.send(f";W20,;M{dev},setTarget,{ch},{target}")
            time.sleep(3)
            dispatch = _nc_lines(nc, nm, rf"^\[DISPATCH\] Maestro slot {slot} \(device {dev}\) <- mesh  cmd 0x04$")
            if len(dispatch) != 1:
                bad.append(f"{len(dispatch)} dispatch lines for the setTarget")
            reached = False
            for _ in range(17):
                if _mae_get(nc, slot, ch) == target:
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
    slot, dev, pos = _usable_slot(nc, _config(nc))
    if not 1 <= dev <= 8:
        raise Skip(f"device {dev} is outside the m1..m8 names W1 accepts from ;M!")
    names = (f"m{dev}pos0", f"m{dev}moving")
    bad = []
    with _debug(nc, DBG_MAESTRO):
        for n in names:
            w.run(f"?VAR,CLEAR,{n}")
        try:
            for verb, name, value in (("getPosition,0", names[0], pos), ("getMovingState", names[1], 0)):
                nm = nc.dev.mark()
                w.send(f";W20,;MG{dev},1,{verb}")
                time.sleep(1.2)
                replies = _nc_lines(nc, nm, rf"^\[DISPATCH\] Maestro {dev} <- mesh query")
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
    local = {dev for _, dev in _local_slots(_config(nc))}
    unhosted = next((d for d in range(1, 9) if d not in local), None)
    cases = [(";M!m1pos0=5", "[DISPATCH] Maestro: ignoring inbound variable push  ;M!m1pos0=5"),
             (";MG5", "[DISPATCH] Maestro: malformed ;MG  ;MG5"),
             (";MG1,25,getPosition,0", "[DISPATCH] Maestro: ;MG bad replyTo 25"),
             (";M1,bogus", "[DISPATCH] Maestro: unparseable inbound  ;M1,bogus"),
             (";M1,bogus" + "x" * 38, "[DISPATCH] Maestro: unparseable inbound  ;M1,bogus" + "x" * 38)]
    if unhosted:
        cases.append((f";MG{unhosted},1,getMovingState", f"[DISPATCH] Maestro: inbound query for device {unhosted} matches no local slot"))
    bad = []
    with _debug(nc, DBG_MAESTRO):
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
        if _nc_lines(nc, nm, r"^\[DISPATCH\] Maestro"):
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
    slot, dev, _ = _usable_slot(nc, _config(nc))
    with _debug(nc, DBG_MAESTRO):
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
    cfg = _config(nc)
    slots = [(s, d, _mae_get(nc, s, 0)) for s, d in _local_slots(cfg)]
    slots = [x for x in slots if isinstance(x[2], int)]
    if not slots:
        raise Skip("no local NaviCore Maestro answers")
    first_by_device = {}
    for s, d, _ in slots:
        first_by_device.setdefault(d, s)
    pfirst = slots[0][2]
    bad = []
    with _debug(nc, DBG_MAESTRO):
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
                _flushed(nc, f"?MAE,{s},0,{p}")
    assert not bad, "; ".join(bad)


@test("navicore.serial_route_dbg", ";W20,;s<n> from W1 reaches NaviCore's aux transmitter (seen through DBG_SERIAL); S0 and S4 are refused", needs=["navicore", "wcb1"], links=[])
def serial_route_dbg(bench):
    """The bytes are physically written to NaviCore's S3-S5; a targeted write is never skipped even when a device owns
    the port (NaviCore.ino:2939-2949), so the test skips when HCR/MP3/DFPlayer/WLED is routed to those ports."""
    nc, w = _nc(bench), usb_wcb(bench)
    cfg = _config(nc)
    if any(cfg.get(k) for k in ("hcrDest", "mp3", "dfp", "wled")):
        bench.note("NaviCore routes a serial device; its aux port receives HIL text in this test")
    labels = ["Serial 3", "Serial 4", "Serial 5"] if cfg.get("boardType") == 1 else ["Serial 1", "Serial 2", "Serial 3"]
    tag = nonce()
    sends = [(f";s1HILA{tag}", f"[DISPATCH] Serial TX [{labels[0]}]  HILA{tag}"), (f";S2HILB{tag}", f"[DISPATCH] Serial TX [{labels[1]}]  HILB{tag}"),
             (f";s3HILC{tag}", f"[DISPATCH] Serial TX [{labels[2]}]  HILC{tag}"),
             (f";s4HILD{tag}", "[DISPATCH] Serial route: S4 is not a NaviCore port (S1-S3)"),
             (f";s0HILE{tag}", "[DISPATCH] Serial route: S0 is not a NaviCore port (S1-S3)")]
    bad = []
    with _debug(nc, DBG_SERIAL):
        for payload, want in sends:
            nm = nc.dev.mark()
            w.send(f";W20,{payload}")
            time.sleep(1.2)
            if want not in [x.rstrip() for x in nc.dev.since(nm)]:
                bad.append(f"{payload[:5]}: no {want!r}")
    assert not bad, "; ".join(bad)


# ============================================================ broadcasts, WCB_SEND and bridged JSON
def _ack(nc, obj, timeout=3.0):
    return nc.json_cmd(obj, r'^\{"type":"ACK"', timeout=timeout).string.rstrip()


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


@test("navicore.plain_broadcast_both_ways", "Plain text broadcast from W1 reaches NaviCore; NaviCore's WCB_SEND target-0 text reaches every eligible WCB port exactly once", needs=["navicore", "wcb1"], links=["W1S1", "W1S2", "W2S1", "W2S2", "W2S3"])
def plain_broadcast_both_ways(bench):
    nc, w = _nc(bench), usb_wcb(bench)
    expected = _expected_ports(bench)
    if not expected:
        raise Skip("no wired WCB port to watch")
    links = [l for l, _ in expected.values()]
    t_a, t_b = f"HILB{nonce()}", f"HILP{nonce()}"
    bad = []
    with _debug(nc, DBG_MAESTRO):                 # the [WCB RX] fallback line is DBG_MAESTRO-gated (NaviCore.ino:3093-3106)
        watch, nm = Watch(*links), nc.dev.mark()
        w.send(t_a)
        time.sleep(3)
        if f"[WCB RX] from WCB1: {t_a}" not in [x.rstrip() for x in nc.dev.since(nm)]:
            bad.append("NaviCore did not log W1's broadcast")
        bad += [f"W1 broadcast, {x}" for x in _port_copies(expected, watch, t_a)]
        watch = Watch(*links)
        ack = _ack(nc, {"type": "WCB_SEND", "target": 0, "cmd": t_b})
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
    ack = _ack(nc, {"type": "WCB_SEND", "target": 0, "cmd": f";S3{t}"}, timeout=1.5)
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
        ack = _ack(nc, {"type": "WCB_SEND", "target": target, "cmd": f";S2HILX{nonce()}"})
        if ack != f'{{"type":"ACK","ok":false,"msg":"target {target} out of range (0=broadcast, 1-20=unicast)"}}':
            bad.append(f"target {target}: {ack}")
    time.sleep(2)
    if watch.got(s12):
        bad.append("a rejected WCB_SEND reached W1 S2")
    long_cmd, fits = ";S3" + padded("L", 185), ";S3" + padded("K", 184)
    watch, nm = Watch(s13, s23), nc.dev.mark()
    _ack(nc, {"type": "WCB_SEND", "target": 0, "cmd": long_cmd})
    time.sleep(3)
    if not _has(nc.dev.since(nm), "[WCB_Client] broadcast: command too long (188 > 187 chars)"):
        bad.append("no library refusal for 188 characters")
    if any(long_cmd[3:].encode() in watch.got(l) for l in (s13, s23)):
        bad.append("the 188-character broadcast was delivered")
    watch = Watch(s13, s23)
    ack = _ack(nc, {"type": "WCB_SEND", "target": 0, "cmd": fits})
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
    ack = _ack(nc, {"type": "WCB_SEND", "target": 0, "cmd": ";S3" + padded("R", 185)})
    assert '"ok":false' in ack, f"a refused broadcast was acknowledged: {ack}"


@test("navicore.wcb_send_fragmented_unicast", "A 300-character WCB_SEND unicast is fragmented by the library and runs exactly once on W1", needs=["navicore", "wcb1"])
def wcb_send_fragmented_unicast(bench):
    s12 = link(bench, 1, "S2")
    nc = _nc(bench)
    cmd = ";S2" + padded("F", 297)
    m, nm = s12.mark(), nc.dev.mark()
    ack = _ack(nc, {"type": "WCB_SEND", "target": 1, "cmd": cmd})
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

    with _debug(nc, DBG_WCB):
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
    with _debug(nc, DBG_MAESTRO | DBG_SERIAL):
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
    cfg = _config(nc)

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
    reset = _ack(nc, {"type": "RESET_MESH_STATS"})
    nc.dev.expect(r"\[WCB\] mesh stats cleared", timeout=3, since=m)
    for i in range(3):
        _ack(nc, {"type": "WCB_SEND", "target": 1, "cmd": f";S2HILS{i}{nonce()}"})
        time.sleep(0.3)
    _ack(nc, {"type": "WCB_SEND", "target": 0, "cmd": f";S3HILS{nonce()}"})
    for _ in range(2):
        wm = w.dev.mark()
        w.send(";W20,?version")
        w.dev.expect(r"^\[TERM:20\]Software Version:", timeout=5, since=wm)
    time.sleep(3)
    m = nc.dev.mark()
    nc.dev.send('{"type":"GET_MESH_STATS"}')
    nc.dev.expect(r'"last":1', timeout=5, since=m)
    pages = [json.loads(x) for x in nc.dev.since(m) if x.startswith('{"type":"MESH_STATS"')]
    rows = {r[0]: r for p in pages for r in p.get("peers", [])}
    agg = pages[0].get("agg", {}) if pages else {}
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
        ack = _ack(nc, {"type": "FORGET_PEER", "id": pid})
        if f"[WCB] WCB {pid} is not a learned peer (nothing to forget)" not in [x.rstrip() for x in nc.dev.since(m)]:
            bad.append(f"id {pid}: no 'not a learned peer' line")
        if ack != f'{{"type":"ACK","of":"FORGET_PEER","ok":true,"id":{pid}}}':
            bad.append(f"id {pid}: ACK {ack}")
    ack0 = _ack(nc, {"type": "FORGET_PEER", "id": 0})
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


def _mode(nc):
    m = nc.dev.mark()
    nc.dev.send("#L12")
    return int(nc.dev.expect(r"Mode=(\d+)", timeout=3, since=m).group(1))


@test("navicore.set_mode", "SET_MODE is mesh-only: unknown over USB; over the mesh it sets the mode, emits rc_mode once, and holds while the SBUS mode switch is still", needs=["navicore", "wcb1"], links=[])
def set_mode(bench):
    """Comment/code gap: rc_telemetry.h's SET_MODE comment says the next SBUS frame overwrites it, but processSbus only
    rewrites the mode when the bound channel moves more than 5 counts (NaviCore.ino:2779), so a mesh SET_MODE holds.
    resetModeAwareKnobs() re-arms mode-aware knobs, so their servos follow the stick in the new mode."""
    nc, w = _nc(bench), usb_wcb(bench)
    m0 = _mode(nc)
    m1 = next(x for x in (1, 2, 3) if x != m0)
    bad = []
    try:
        usb = nc.json_cmd({"type": "SET_MODE", "mode": m1}, r'^\{"type":"ERROR"', timeout=2).string
        if '"msg":"unknown type"' not in usb or _mode(nc) != m0:
            bad.append(f"USB SET_MODE: {usb}")
        wm = w.dev.mark()
        w.send(f';W20,{{"type":"SET_MODE","mode":{m1}}}')
        time.sleep(2)
        if _mode(nc) != m1:
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
        if _mode(nc) != m1 or _has(w.dev.since(wm), '"type":"rc_mode"'):
            bad.append("mode 4 was accepted")
        time.sleep(5)
        if _mode(nc) != m1:
            bad.append("the SBUS mode switch overrode the mesh SET_MODE while still")
    finally:
        w.send(f';W20,{{"type":"SET_MODE","mode":{m0}}}')
        time.sleep(2)
    assert _mode(nc) == m0, "the original mode was not restored"
    assert not bad, "; ".join(bad)


@test("navicore.test_action_usb", "USB TEST_ACTION fires wcb_unicast, wcb_broadcast and serial actions with exact dispatch lines and bytes on the wire", needs=["navicore", "wcb1"])
def test_action_usb(bench):
    """TEST_ACTION bypasses the calibration and replay gates; never record/play/stop. The serial action writes
    NaviCore's first aux port."""
    s12, s13, s23 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 2, "S3")
    nc = _nc(bench)
    label = "Serial 3" if _config(nc).get("boardType") == 1 else "Serial 1"
    tag = nonce()
    acks = []
    with _debug(nc, DBG_WCB | DBG_SERIAL):
        watch, nm = Watch(s12, s13, s23), nc.dev.mark()
        acks.append(_ack(nc, {"type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": "1", "cmd": f";S2HILT{tag}"}}))
        watch.expect(s12, f"HILT{tag}\r".encode(), timeout=3)
        acks.append(_ack(nc, {"type": "TEST_ACTION", "action": {"type": "wcb_broadcast", "cmd": f";S3HILU{tag}"}}))
        watch.expect(s13, f"HILU{tag}\r".encode(), timeout=3)
        watch.expect(s23, f"HILU{tag}\r".encode(), timeout=3)
        acks.append(_ack(nc, {"type": "TEST_ACTION", "action": {"type": "serial", "port": "S3", "cmd": f"HILV{tag}"}}))
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
    with _debug(nc, DBG_WCB):
        for obj in ({"type": "TEST_ACTION"}, {"type": "TEST_ACTION", "action": {"type": "smooth"}}):
            if _ack(nc, obj) != no:
                bad.append(f"{obj} was accepted")
        watch, nm = Watch(s12), nc.dev.mark()
        for target in ("25", "abc"):
            _ack(nc, {"type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": target, "cmd": f";S2HILZ{nonce()}"}})
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
    ack = _ack(nc, {"type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": "25", "cmd": f";S2HILZ{nonce()}"}})
    assert '"ok":false' in ack, f"an action that did nothing was acknowledged: {ack}"


# ============================================================ TRIGGER, record/replay, #L diagnostics, a temporary probe
@test("navicore.trigger_bounds_usb_vs_mesh", "TRIGGER ranges: USB rejects a bad button or tap; the mesh clamps tap (9 -> 4, 0 -> 1) and drops a bad button", needs=["navicore", "wcb1"], links=[])
def trigger_bounds_usb_vs_mesh(bench):
    nc, w = _nc(bench), usb_wcb(bench)
    if "136" in _config(nc).get("mappings", {}):
        raise Skip("slot 136 (mode 1, button 36) is mapped: its tiers would fire real actions")
    bad = []
    for btn, tap in ((37, 1), (36, 5), (36, 0)):
        m = nc.dev.mark()
        ack = _ack(nc, {"type": "TRIGGER", "mode": 1, "btn": btn, "tap": tap})
        if ack != '{"type":"ACK","ok":false,"msg":"bad mode/btn/tap"}' or _has(nc.dev.since(m), '"type":"rc_trig"'):
            bad.append(f"USB btn {btn} tap {tap}: {ack}")
    m = nc.dev.mark()
    ack = _ack(nc, {"type": "TRIGGER", "mode": 1, "btn": 36, "tap": 1})
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
    cfg = _config(nc)
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
    with config_guard(bench, 1, 2), _debug(nc, DBG_MAESTRO | DBG_WCB):
        m = nc.dev.mark()
        ack = _ack(nc, {"type": "TRIGGER", "mode": mode, "btn": btn, "tap": 1})
        time.sleep(longest / 1000 + 2)
        lines = [x.rstrip() for x in nc.dev.since(m)]
    assert f"[TRIGGER] mode={mode} btn={btn} tap=1" in lines, "no [TRIGGER] line"
    positions = [next((i for i, x in enumerate(lines) if x == e), None) for e in expected]
    assert None not in positions and positions == sorted(positions), f"expected {expected} in order, NaviCore printed {lines}"
    assert ack == '{"type":"ACK","ok":true}', ack


def _rec_info(nc):
    m = nc.dev.mark()
    nc.dev.send("?REC,INFO")
    g = nc.dev.expect(r"^\[REC\] state=(\S+)  events=(\d+)/(\d+)  dur=(\d+)ms  drops=(\d+)  buf=(\S+)", timeout=3, since=m)
    return g.groups()


def _clips(nc):
    m = nc.dev.mark()
    nc.dev.send("?REC,LS")
    nc.dev.expect(r"^\[CLIPLIST:END\]", timeout=5, since=m)
    lines = nc.dev.since(m)
    return lines, [json.loads(x[len("[CLIPITEM]"):]) for x in lines if x.startswith("[CLIPITEM]")]


@test("navicore.rec_info_list", "Record/replay observability: ?REC,INFO and ?REC,LS markers, and PLAY of a missing clip, with no flash writes", needs=["navicore"], links=[])
def rec_info_list(bench):
    """Read-only. Never ?REC,START/SAVE/RM/RENAME/EDIT*/CLEAR/LOAD: they write flash or replace the RAM take."""
    nc = _nc(bench)
    before = _rec_info(nc)
    if before[0] != "idle":
        raise Skip(f"NaviCore's recorder is {before[0]}")
    lines, items = _clips(nc)
    missing = f"HILnope{nonce()}"
    m = nc.dev.mark()
    nc.dev.send(f"?REC,PLAY,{missing}")
    nc.dev.expect(rf"^\[REC\] clip '{missing}' not found", timeout=3, since=m)
    after = _rec_info(nc)
    assert "[REC] clips:" in [x.rstrip() for x in lines] and "[CLIPLIST:BEGIN]" in [x.rstrip() for x in lines], lines[:4]
    assert all({"name", "bytes", "dur", "n"} <= set(i) for i in items), items
    assert after == before, f"INFO changed: {before} -> {after}"


@test("navicore.rec_play_clip", "OPT-IN (navicore_clip, attended): play a saved clip of up to 30 s; the start line, REPLAYING state and completion timing", needs=["navicore", "wcb1"], links=[], opt_in="navicore_clip")
def rec_play_clip(bench):
    """Drives every Maestro channel in the clip and re-fires its recorded actions, possibly WCB commands (hence
    config_guard). The clip must hold no record/play/stop action: a recorded record would start a take that saves."""
    nc = _nc(bench)
    state = _rec_info(nc)
    if state[0] != "idle" or state[1] != "0":
        raise Skip("the recorder is busy or holds an unsaved take")
    clip = next((c for c in _clips(nc)[1] if c["dur"] <= 30000), None)
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
            during = _rec_info(nc)
            nc.dev.expect(r"^\[REC\] ▶ playback complete", timeout=dur / 1000 + 5, since=m)
            took = (next(ts for ts, x in list(nc.dev.lines[m:]) if "playback complete" in x) - started) * 1000
            after = _rec_info(nc)
        finally:
            if _rec_info(nc)[0] == "REPLAYING":
                nc.dev.send("?REC,STOP")
                time.sleep(0.5)
    bench.note(f"clip {clip['name']}: {events} events over {dur} ms, completed after {took:.0f} ms")
    assert events == clip["n"], f"replaying {events} events, the clip lists {clip['n']}"
    assert during[0] == "REPLAYING", during
    assert dur - 50 <= took <= dur + 1500, f"completed after {took:.0f} ms for a {dur} ms clip"
    assert after[0] == "idle", after


def _cli(nc, cmd, pattern, timeout=3.0):
    m = nc.dev.mark()
    nc.dev.send(cmd)
    nc.dev.expect(pattern, timeout=timeout, since=m)
    time.sleep(0.3)
    return [x.rstrip() for x in nc.dev.since(m)]


@test("navicore.cli_codes", "#L diagnostics: an unknown code, lowercase, #L1, the #L11 board list against GET_WCB_STATUS, the #L13 raw frame", needs=["navicore"], links=[])
def cli_codes(bench):
    """Never #L2/#L02 (restart) or #L20/#L21 (HCR test frames); #L10 is navicore.sbus_live_dump_toggle."""
    nc = _nc(bench)
    st = nc.wcb_status()
    known = [i + 1 for i, v in enumerate(st.get("known", [])) if v]
    online, clients = st.get("online", []), st.get("clients", [0] * 20)
    bad = []
    if "Unknown #L code 99. Valid: 1,2,9,10,11,12,13,20,21" not in _cli(nc, "#L99", r"Unknown #L code 99"):
        bad.append("#L99")
    if not any(re.match(r"^Mode=\d+  matrixBtn=\S+  matrixVal=\S+$", x) for x in _cli(nc, "#l12", r"Mode=\d+")):
        bad.append("#l12")
    # #L1 names the booted profile (navicore.l1_names_board_profile), so either name is correct here.
    if not any(x in ("NaviCore — WCB HW 3.2", "NaviCore — NaviCore v2") for x in _cli(nc, "#L1", r"NaviCore — ")):
        bad.append("#L1")
    l11 = _cli(nc, "#L11", r"Board status: ")
    if f"WCB device ID: 20  quantity: {st.get('quantity')}" not in l11:
        bad.append(f"#L11 header {l11[:2]}")
    status = next((x for x in l11 if x.startswith("  Board status: ")), "")
    boards = {int(i): up for i, up, _ in re.findall(r"WCB(\d+)=(UP|dn)(\*?)", status)}
    if set(boards) != set(known):
        bad.append(f"#L11 lists {sorted(boards)}, GET_WCB_STATUS knows {known}")
    bad += [f"#L11 WCB{i}={up} vs online {online[i - 1]}" for i, up in boards.items()
            if i <= len(online) and not clients[i - 1] and (up == "UP") != bool(online[i - 1])]   # arrays end at the highest known id
    raw = _cli(nc, "#L13", r"^---- SBUS RAW ---- \(\d+ bytes, SBUS-\d+\)")
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
    if _config(nc).get("boardType") == 1:
        raise Skip("the board profile is WCB HW 3.2, where the line is right")
    line = next((x for x in _cli(nc, "#L1", r"NaviCore — ") if x.startswith("NaviCore — ")), "")
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
        m = nc.dev.mark()
        nc.dev.send("?WDP,DUMP")
        nc.dev.expect(r"^\[WDP:END,count=\d+\]", timeout=5, since=m)
        line = next((x for x in nc.dev.since(m) if x.startswith("[WDPCFG:")), "")
        g = re.search(r"PEERS=(\d+)", line)
        return int(g.group(1)) if g else None

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
SBUS_MIN, SBUS_CENTER, SBUS_MAX = 172, 992, 1811          # SBUSController.ino:113-115


def _sbus_send(dev, obj):
    dev.send(json.dumps(obj, separators=(",", ":")))       # every line starts with '{': bare 'm' and 'w' save to flash


def _sbus_ping(dev, timeout=60.0):
    deadline = time.monotonic() + timeout
    while True:
        m = dev.mark()
        _sbus_send(dev, {"t": "ping"})
        try:
            return dev.expect(r'^\{"t":"pong"', timeout=3, since=m)
        except AssertionError:
            if time.monotonic() > deadline:
                raise


def _sbus_cfg(dev):
    """getcfg reaches serial only within 5 s of a ping (SBUSController.ino:1001-1029). The line carries WiFi credentials."""
    _sbus_ping(dev)
    m = dev.mark()
    _sbus_send(dev, {"t": "getcfg"})
    return json.loads(dev.expect(r'^\{"e":"cfg"', timeout=5, since=m).string)


def _sbus_bootlog(dev):
    """The controller's RTC boot record (SBUSController.ino:1090-1096, 1699-1727): n counts boots since the last power
    loss (RTC_NOINIT, :1571-1678) and up is this boot's millis(). The reply is not gated on a ping."""
    _sbus_ping(dev)
    m = dev.mark()
    _sbus_send(dev, {"t": "bootlog"})
    return json.loads(dev.expect(r'^\{"e":"bootlog"', timeout=5, since=m).string)


def _l09(nc):
    m = nc.dev.mark()
    nc.dev.send("#L09")
    # NaviCore holds a finished block until more output follows it (see the header), so poke it with a
    # harmless #L12: without this the SBUS STATE block sits unsent and the wait below times out while
    # the block arrives the moment the NEXT test writes. Measured at 3.07 s held (run 20260916-135810,
    # sbus.btn_hold_unconfigured_long: the 32.838 s block and the 35.843 s one both landed at 35.905).
    time.sleep(0.15)
    nc.dev.send("#L12")
    nc.dev.expect(r"^\s*CH17-24:", timeout=3, since=m)
    text = nc.dev.since(m)
    joined = "\n".join(text)
    channels = [int(v) for t in text for hit in [re.match(r"^\s*CH\d+-\d+:\s+(.*)$", t)] if hit for v in hit.group(1).split()]
    grab = lambda rx: (re.search(rx, joined).group(1) if re.search(rx, joined) else None)
    return dict(fps=int(grab(r"fps=(\d+)") or 0), variant=grab(r"variant=(\S+)"), frames=grab(r"frames=(\d+)"),
                age=int(grab(r"ageMs=(\d+)") or 0), channels=channels, text=joined)


def _entries(obj):
    return list(obj.values()) if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])


def _channel_of(entry):
    return entry.get("channel", entry.get("c", entry.get("ch"))) if isinstance(entry, dict) else None


def _safe_channels(ncfg):
    """SBUS channels NaviCore binds to nothing. Conservative: every channel a switch or knob names is unsafe, as is the
    matrix channel (their inner key names were not re-read, so they are parsed leniently)."""
    unsafe = {ncfg.get("matrixChannel")}
    unsafe |= {_channel_of(e) for e in _entries(ncfg.get("switches")) + _entries(ncfg.get("knobs"))}
    unsafe.discard(None)
    return set(range(1, 25)) - unsafe


def _sbus_setup(bench):
    dev, nc = bench.dev("sbus"), _nc(bench)
    state = _l09(nc)
    if state["fps"] < 100 or state["variant"] != "SBUS-24":
        raise Skip(f"NaviCore sees no full-rate SBUS-24 stream (fps {state['fps']}, {state['variant']})")
    return dev, nc, _sbus_cfg(dev), _config(nc)


def _band(ncfg, value):
    """The matrix slot whose threshold band holds <value> (first match, 0/0 bands inert; NaviCore.ino:568-579)."""
    for i, t in enumerate(ncfg.get("thresholds", [])):
        lo, hi = (t.get("minPwm", t.get("min")), t.get("maxPwm", t.get("max"))) if isinstance(t, dict) else (t[0], t[1])
        if (lo, hi) != (0, 0) and lo is not None and hi is not None and lo <= value <= hi:
            return i + 1
    return None


def _matrix_button(bench, nc, cfg, ncfg, mode):
    """(index, button, slot) of a controller button on NaviCore's matrix channel that decodes to an unmapped slot in
    <mode>, where both the channel's resting value and the release value 992 decode to no slot."""
    mc = ncfg.get("matrixChannel")
    rest = _l09(nc)["channels"][mc - 1]
    if _band(ncfg, rest) or _band(ncfg, SBUS_CENTER):
        raise Skip("the matrix channel's resting or release value already decodes to a slot")
    for i, b in enumerate(cfg.get("btn", [])):
        slot = _band(ncfg, b.get("v", 0)) if b.get("c") == mc else None
        if slot and str(mode * 100 + slot) not in ncfg.get("mappings", {}):
            return i, b, slot
    raise Skip("no controller button on the matrix channel decodes to an unmapped slot")


def _rc_trigs(nc, since, slot=None):
    """(timestamp, rc_trig object) for <slot>, or for every slot when <slot> is None."""
    return [(ts, json.loads(x)) for ts, x in list(nc.dev.lines[since:])
            if '"type":"rc_trig"' in x and (slot is None or f'"btn":{slot},' in x)]


@test("sbus.discover", "SBUS controller config and NaviCore bindings read back; the safe channels are computed; NaviCore sees SBUS-24 at full rate", needs=["sbus", "navicore"], links=[])
def discover(bench):
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    missing = [k for k in ("sbus24", "aMin", "aMax", "aRev", "sw", "sl", "tr", "btn", "lua") if k not in cfg]
    safe = _safe_channels(ncfg)
    bench.note(f"SBUS channels NaviCore binds nothing to: {sorted(safe)}")
    assert not missing, f"controller getcfg lacks {missing}"
    assert "matrixChannel" in ncfg and "mappings" in ncfg, "NaviCore GET_CONFIG lacks matrixChannel or mappings"


@test("sbus.btn_single_tap", "A controller matrix button: NaviCore decodes the slot and emits exactly one tap-1 rc_trig tapWindowMs after release, also relayed to W1", needs=["sbus", "navicore", "wcb1"], links=[])
def btn_single_tap(bench):
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    w = usb_wcb(bench)
    mode = _mode(nc)
    i, button, slot = _matrix_button(bench, nc, cfg, ncfg, mode)
    tap_ms = ncfg.get("tapWindowMs", 500)
    rc = f'{{"sys":1,"type":"rc_trig","id":20,"mode":{mode},"btn":{slot},"tap":1}}'
    w.send(';W20,{"type":"PING"}')                      # opens W1's 20 s relay window
    time.sleep(1.0)
    try:
        nm, wm = nc.dev.mark(), w.dev.mark()
        _sbus_send(dev, {"t": "btn", "i": i, "p": True})
        pressed = time.monotonic()
        time.sleep(0.15)
        held = _cli(nc, "#L12", r"Mode=\d+")
        time.sleep(max(0.0, pressed + 0.2 - time.monotonic()))
        _sbus_send(dev, {"t": "btn", "i": i, "p": False})
        released = time.monotonic()
        time.sleep(tap_ms / 1000 + 1.5)
        trigs = _rc_trigs(nc, nm, slot)
        relayed = _has(w.dev.since(wm), rc)
        after = _l09(nc)["channels"][ncfg["matrixChannel"] - 1]
    finally:
        _sbus_send(dev, {"t": "btn", "i": i, "p": False})
    bench.note(f"rc_trig relayed to W1: {relayed} (best-effort broadcast)")
    assert f"Mode={mode}  matrixBtn={slot}  matrixVal={button['v']}" in held, f"while held NaviCore said {held}"
    assert len(trigs) == 1 and trigs[0][1]["tap"] == 1, f"rc_trig lines {trigs}"
    late = (trigs[0][0] - released) * 1000
    assert tap_ms - 50 <= late <= tap_ms + 300, f"rc_trig {late:.0f} ms after release (tapWindowMs {tap_ms})"
    assert after == SBUS_CENTER, f"the matrix channel reads {after} after release"


@test("sbus.btn_double_triple_tap", "Two and three quick presses give one rc_trig with tap 2 / tap 3, timed from the last press", needs=["sbus", "navicore"], links=[])
def btn_double_triple_tap(bench):
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    mode = _mode(nc)
    i, _, slot = _matrix_button(bench, nc, cfg, ncfg, mode)
    tap_ms = ncfg.get("tapWindowMs", 500)
    bad = []
    try:
        for presses in (2, 3):
            nm = nc.dev.mark()
            for _ in range(presses):
                _sbus_send(dev, {"t": "btn", "i": i, "p": True})
                last = time.monotonic()
                time.sleep(0.1)
                _sbus_send(dev, {"t": "btn", "i": i, "p": False})
                time.sleep(0.12)
            time.sleep(tap_ms / 1000 + 2)
            trigs = _rc_trigs(nc, nm, slot)
            if [t["tap"] for _, t in trigs] != [presses]:
                bad.append(f"{presses} presses gave taps {[t['tap'] for _, t in trigs]}")
            elif not tap_ms - 50 <= (trigs[0][0] - last) * 1000 <= tap_ms + 300:
                bad.append(f"{presses} presses: rc_trig {(trigs[0][0] - last) * 1000:.0f} ms after the last press")
    finally:
        _sbus_send(dev, {"t": "btn", "i": i, "p": False})
    assert not bad, "; ".join(bad)


@test("sbus.btn_hold_unconfigured_long", "Holding a button whose slot has no tier-4 mapping still gives tap 1 on release, no long press", needs=["sbus", "navicore"], links=[])
def btn_hold_unconfigured_long(bench):
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    mode = _mode(nc)
    i, _, slot = _matrix_button(bench, nc, cfg, ncfg, mode)
    tap_ms, hold_ms = ncfg.get("tapWindowMs", 500), ncfg.get("holdMs", 750)
    try:
        nm = nc.dev.mark()
        _sbus_send(dev, {"t": "btn", "i": i, "p": True})
        time.sleep((hold_ms + 500) / 1000)
        while_held = _rc_trigs(nc, nm, slot)
        _sbus_send(dev, {"t": "btn", "i": i, "p": False})
        released = time.monotonic()
        time.sleep(tap_ms / 1000 + 1.5)
        trigs = _rc_trigs(nc, nm, slot)
    finally:
        _sbus_send(dev, {"t": "btn", "i": i, "p": False})
    assert not while_held, f"rc_trig while held: {while_held}"
    assert [t["tap"] for _, t in trigs] == [1], f"taps {[t['tap'] for _, t in trigs]}"
    assert tap_ms - 50 <= (trigs[0][0] - released) * 1000 <= tap_ms + 300, "tap 1 not timed from the release"


@test("sbus.mode_sets_trigger_mode", "After a mesh SET_MODE, a controller matrix press dispatches under the new mode", needs=["sbus", "navicore", "wcb1"], links=[])
def mode_sets_trigger_mode(bench):
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    w = usb_wcb(bench)
    m0 = _mode(nc)
    i, _, slot = _matrix_button(bench, nc, cfg, ncfg, m0)
    m1 = next((m for m in (1, 2, 3) if m != m0 and str(m * 100 + slot) not in ncfg.get("mappings", {})), None)
    if m1 is None:
        raise Skip("the chosen slot is mapped in every other mode")
    tap_ms = ncfg.get("tapWindowMs", 500)
    try:
        w.send(f';W20,{{"type":"SET_MODE","mode":{m1}}}')
        time.sleep(2)
        if _mode(nc) != m1:
            raise AssertionError("the mesh SET_MODE did not take")
        nm = nc.dev.mark()
        _sbus_send(dev, {"t": "btn", "i": i, "p": True})
        time.sleep(0.2)
        _sbus_send(dev, {"t": "btn", "i": i, "p": False})
        time.sleep(tap_ms / 1000 + 1.5)
        trigs = _rc_trigs(nc, nm, slot)
    finally:
        _sbus_send(dev, {"t": "btn", "i": i, "p": False})
        w.send(f';W20,{{"type":"SET_MODE","mode":{m0}}}')
        time.sleep(2)
    assert [(t["mode"], t["tap"]) for _, t in trigs] == [(m1, 1)], f"rc_trig lines {trigs}"
    assert _mode(nc) == m0, "the original mode was not restored"


@test("sbus.switch_exact", "A controller switch position arrives at NaviCore as its exact configured value; a 2-position switch's middle snaps low", needs=["sbus", "navicore"], links=[])
def switch_exact(bench):
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    safe = _safe_channels(ncfg)
    k, sw = next(((k, s) for k, s in enumerate(cfg.get("sw", [])) if s.get("c") in safe), (None, None))
    if sw is None:
        raise Skip("no controller switch on a channel NaviCore leaves unbound")
    c, v, p0 = sw["c"], sw["v"], sw.get("pos", 0)
    base = _l09(nc)["channels"]
    got = {}
    try:
        for p in (0, 1, 2):
            _sbus_send(dev, {"t": "sw", "i": k, "p": p})
            time.sleep(0.4)
            got[p] = _l09(nc)["channels"]
    finally:
        _sbus_send(dev, {"t": "sw", "i": k, "p": p0})
        time.sleep(0.4)
    want = {0: v[0], 1: v[1], 2: v[2]} if sw.get("t", 0) == 0 else {0: v[0], 1: v[0], 2: v[2]}
    for p in (0, 1, 2):
        assert got[p][c - 1] == want[p], f"position {p}: CH{c} = {got[p][c - 1]}, expected {want[p]}"
        moved = [n + 1 for n in range(24) if n != c - 1 and got[p][n] != base[n]]
        assert not moved, f"position {p} also moved CH{moved}"
    assert _l09(nc)["channels"][c - 1] == base[c - 1], "the switch channel did not return to its value"


@test("sbus.slider_exact", "A controller slider's percent arrives at NaviCore as its exact SBUS value (0/25/50/75/100 %, out-of-range clamped)", needs=["sbus", "navicore"], links=[])
def slider_exact(bench):
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    safe = _safe_channels(ncfg)
    j, sl = next(((j, s) for j, s in enumerate(cfg.get("sl", [])) if s.get("c") in safe), (None, None))
    if sl is None:
        raise Skip("no controller slider on a channel NaviCore leaves unbound")
    c, pct0 = sl["c"], sl.get("pct", 50)
    want = {0: 172, 25: 582, 50: 992, 75: 1401, 100: 1811, 150: 1811, -5: 172}     # (uint16)(pct/100*1639 + 172 + 0.5)
    bad = []
    try:
        for pct, value in want.items():
            _sbus_send(dev, {"t": "sl", "i": j, "v": pct})
            time.sleep(0.4)
            got = _l09(nc)["channels"][c - 1]
            if got != value:
                bad.append(f"{pct} %: CH{c} = {got}, expected {value}")
    finally:
        _sbus_send(dev, {"t": "sl", "i": j, "v": pct0})
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
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    safe = _safe_channels(ncfg)
    t, tr = next(((t, x) for t, x in enumerate(cfg.get("tr", [])) if x.get("c") in safe), (None, None))
    mc, maps = ncfg.get("matrixChannel"), ncfg.get("mappings", {})
    m0 = _mode(nc)
    m1, sR, sL = m0, None, None
    if tr is None:
        if not mc:
            raise Skip("NaviCore's GET_CONFIG names no matrix channel")
        if _band(ncfg, _l09(nc)["channels"][mc - 1]) or _band(ncfg, SBUS_CENTER):
            raise Skip("the matrix channel's resting or release value already decodes to a slot")
        for m in [m0] + [x for x in (1, 2, 3) if x != m0]:
            for i, x in enumerate(cfg.get("tr", [])):
                if x.get("c") != mc or x.get("m", 0) != 1:
                    continue
                # Both directions must decode, to two different slots: a None slot or a repeat has no exact rc_trig list.
                a, b = _band(ncfg, x.get("vR", 0)), _band(ncfg, x.get("vL", 0))
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

    def read(cmd):
        _sbus_send(dev, cmd)
        time.sleep(0.4)
        return _l09(nc)["channels"][c - 1]

    try:
        if m1 != m0:
            w.send(f';W20,{{"type":"SET_MODE","mode":{m1}}}')
            time.sleep(2)
            if _mode(nc) != m1:
                raise AssertionError("the mesh SET_MODE did not take")
        nm = nc.dev.mark()
        if mode == 0:
            up, down = read({"t": "tr", "i": t, "d": 1}), read({"t": "tr", "i": t, "d": -1})
            assert up == min(cur0 + step, SBUS_MAX), f"step up: CH{c} = {up}"
            if cur0 + step <= SBUS_MAX:
                assert down == cur0, f"step back: CH{c} = {down}, expected {cur0}"
        else:
            got = [read({"t": "tr", "i": t, "d": 1, "p": True}), read({"t": "tr", "i": t, "d": 1, "p": False}),
                   read({"t": "tr", "i": t, "d": -1, "p": True}), read({"t": "tr", "i": t, "d": -1, "p": False})]
            if sR:
                time.sleep(ncfg.get("tapWindowMs", 500) / 1000 + 1.0)    # the last tap fires tapWindowMs after release
                trigs = [(x["mode"], x["btn"], x["tap"]) for _, x in _rc_trigs(nc, nm)]   # every slot, not just these
    finally:
        if mode == 1:
            _sbus_send(dev, {"t": "tr", "i": t, "d": 1, "p": False})    # a release centres the trim either way
        if m1 != m0:
            w.send(f';W20,{{"type":"SET_MODE","mode":{m0}}}')
            time.sleep(2)
    assert _mode(nc) == m0, "the original mode was not restored"
    if mode == 1:
        assert got == [tr.get("vR"), SBUS_CENTER, tr.get("vL"), SBUS_CENTER], f"button-mode trim values {got}"
    if sR:
        assert trigs == [(m1, sR, 1), (m1, sL, 1)], f"rc_trig (mode, btn, tap) {trigs}, expected slots {sR} then {sL} in mode {m1}"


@test("sbus.signal_loss_controller_reset", "OPT-IN (sbus_reset): resetting the controller stops SBUS frames; NaviCore's fps drops to 0 with flags unchanged and no dispatch, then recovers", needs=["sbus", "navicore"], links=[], opt_in="sbus_reset")
def signal_loss_controller_reset(bench):
    """The reset is RTS=1/DTR=0 on the controller's USB-Serial/JTAG port, which resets the chip. On Windows, usbser.sys
    sends SET_CONTROL_LINE_STATE only when DTR is written, so an RTS change alone never reaches the board: DTR is
    re-written after every RTS change, as esptool's _setRTS does (esptool/reset.py:72-77). DTR stays 0, so the chip
    boots the app, not download mode. The controller's boot record proves the reset happened before NaviCore is blamed.
    Its boot resets its runtime state, so the test only runs when that state already equals the boot defaults."""
    dev, nc, cfg, ncfg = _sbus_setup(bench)
    if (any(s.get("pos") != s.get("d") for s in cfg.get("sw", [])) or any(s.get("pct") != 50 for s in cfg.get("sl", []))
            or any(x.get("cur") != SBUS_CENTER for x in cfg.get("tr", []))):
        raise Skip("the controller is not in its boot state; a reset would move channels")
    base = _l09(nc)
    b0 = _sbus_bootlog(dev)
    nm = nc.dev.mark()
    t_pulse = time.monotonic()
    # A local handle: if the USB re-enumerates, the reader's _reopen() sets dev._ser = None before the release below.
    # A closed handle is harmless - pyserial skips the hardware call once is_open is false.
    s = dev._ser
    s.rts = True
    s.dtr = False
    time.sleep(0.2)
    try:
        s.rts = False
        s.dtr = False
    except (serial.SerialException, OSError):
        pass    # the port went away with the reset; it is reopened below
    lost, deadline = None, time.monotonic() + 3
    while time.monotonic() < deadline:
        state = _l09(nc)
        if state["fps"] == 0:
            lost = state
            break
        time.sleep(0.5)
    time.sleep(1.0)
    later = _l09(nc)
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
    _sbus_ping(reopened)
    b1 = _sbus_bootlog(reopened)
    time.sleep(3)
    recovered = _l09(nc)
    assert not _has(boot, "WiFi section missing from config — upgrading file."), "the controller rewrote its config on boot: report it"
    # Not the reset reason: which RTC code this reset reports is unverified here, and n counts it whatever it is.
    assert b1["n"] > b0["n"] or b1["up"] < (time.monotonic() - t_pulse) * 1000, (
        f"the controller did not reset: boot count {b0['n']}->{b1['n']}, up {b1['up']} ms ({b1.get('rstn')}, {b1.get('rtcn')})")
    assert lost, "NaviCore's fps never dropped to 0 after the reset"
    assert "lost=no" in lost["text"] and "failsafe=no" in lost["text"], "the frame flags changed without a decoded frame"
    assert later["age"] > lost["age"], f"ageMs did not grow: {lost['age']} -> {later['age']}"
    assert not outage_trigs, f"dispatch during the outage: {outage_trigs}"
    assert recovered["fps"] >= 100 and recovered["variant"] == base["variant"], f"after the reset: fps {recovered['fps']}, {recovered['variant']}"
