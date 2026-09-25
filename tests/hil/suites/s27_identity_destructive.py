"""Identity and radio settings, and the destructive-but-restorable commands (docs/HIL_TEST_AUDIT.md WP3 / WP4).

Everything here changes W1 through its own USB console and puts it back the same way, so a board that is unreachable
over the mesh for a moment is still reachable to the test. Rules:
- W1's WDP is off while W1 is WCB 3, so no board or client can learn a WCB 3 (W2 would persist it; NaviCore would
  auto-join it): auto-join only ever runs from a WDP advert (WCB_WDP.cpp, addActivePeer), and heartbeats persist nothing.
- ?WCBCH and ?WCB apply at boot, ?HW only at boot (the pin map), ?MAC,2 at once (the receive filter). ?HW is set and
  put back WITHOUT a reboot in between: booting W1 on another board's pin map would take its ports away.
- ?EPASS and ?ERASE,NVS are not sent: the first would put the mesh credential in test code, the second needs the
  whole factory chain replayed and loses W1's persisted learned peers and device records. Both stay attended-only
  (HIL_TEST_AUDIT.md §7).
- Restore if aborted, on W1's USB console: ?WCB,1 / ?WCBCH,<chain value> / ?MAC,2,<chain value> / ?HW,<chain value>,
  then ?reboot; ?WDP,ON; ?WDP,AUTOJOIN,ON; ?WDP,ADD,<n> for each learned peer the run log lists. Nothing here moves a servo.
"""
import re
import time

from hil.runner import Skip, test
from suites.common import Console, Watch, config_guard, link, marker, require_tokens, snapshot, token, usb_wcb
from suites.s14_pwm import _no_pwm, _pwm_reboot
from suites.s22_maestro_kyber import _kyber_list, _require_kyber_broadcast_remote, _restore_remote, _wdp_off


def _has(lines, text):
    return any(text in x for x in lines)


def _in_order(lines, wanted, label):
    i = 0
    for n in wanted:
        while i < len(lines) and n not in lines[i]:
            i += 1
        if i == len(lines):
            return [f"{label}: missing '{n}' (in order) - got {lines}"]
        i += 1
    return []


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _dump_rows(w):
    """{N: PEER} for every [WDP:N=...] row of W1's DUMP (neighbours that have advertised, plus the self row)."""
    out = {}
    for x in w.run("?WDP,DUMP", timeout=8):
        m = re.match(r"^\[WDP:N=(\d+),.*PEER=(\d+)\]", x.rstrip())
        if m:
            out[int(m.group(1))] = int(m.group(2))
    return out


def _live_peers(w):
    """The count ?PEERSLIVE reports: the WCBQ floor plus the learned peers (activePeerCount, WCB.ino)."""
    for x in w.run("?PEERSLIVE"):
        m = re.match(r"^Live peers: (\d+) \(WCBQ floor (\d+)", x)
        if m:
            return int(m.group(1))
    raise AssertionError("?PEERSLIVE printed no 'Live peers' line")


def _learned_peers(w, floor):
    """The ids above the WCBQ floor that W1 will unicast to, i.e. its learned peers. Nothing lists them by id, so each
    candidate is asked for: a ;W<n> to a non-member prints the refusal instead of sending (WCB.ino:7091); a member gets
    one ?PEERSLIVE, which is read-only wherever it lands (a learned peer is normally a board that is off right now, so
    the send just retries and fails). The controller id is never a learned peer (addActivePeer refuses it)."""
    out = []
    for n in range(floor + 1, 20):
        if not _has(w.run(f";W{n},?PEERSLIVE"), f"WCB {n} is not a reachable target"):
            out.append(n)
    return out


# ============================================================ identity
@test("ident.wcb_number_reboot", "?WCB,3 renumbers W1 at the next boot: the chain says ?WCB,3, the WDP self row and the ETM heartbeats say 3; ?WCB,21 is refused; ?WCB,1 and a reboot put it back (WDP off; 2 reboots)", needs=["wcb1"], links=[])
def wcb_number_reboot(bench):
    """saveWCBNumberToPreferences (WCB_Storage.cpp) takes effect live for the number itself (so a push's later MAESTRO
    lines use it) and at boot for the radio address, peers and ETM."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    if me != 1:
        raise Skip("the console board is not WCB 1")
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        if token(before[1], "?WCBQ,") != "?WCBQ,2":
            raise Skip("W1's WCBQ is not 2")
        rows2 = {int(m.group(1)) for x in _crun(c2, "?WDP,DUMP", 1.5) for m in [re.match(r"^\[WDP:N=(\d+),", x)] if m}
        if 3 in rows2:
            raise Skip("W2 already knows a WCB 3")
        renumbered = False
        with _wdp_off(w, before[1]):
            try:
                out = w.run("?WCB,21")
                if not _has(out, "Invalid WCB number 21. Valid range: 1-20."):
                    problems.append(f"?WCB,21 printed {out}")
                out = w.run("?WCB,3")
                renumbered = True
                problems += _in_order(out, ["Changed WCB Number to: 3", "Please reboot to take full effect"], "?WCB,3")
                if "?WCB,3" not in snapshot(bench, 1):
                    problems.append("the chain does not say ?WCB,3")
                w.reboot()
                if not _has(w.run("?WDP,DUMP", timeout=8), "[WDP:N=3,"):
                    problems.append("after the reboot the WDP self row is not N=3")
                w.run("?DEBUG,ETM,ON")
                m = w.dev.mark()
                try:
                    w.dev.expect(r"^\[ETM\] Heartbeat sent \(WCB3\)", timeout=30, since=m)
                except AssertionError:
                    problems.append("no ETM heartbeat as WCB3 within 30 s of the reboot")
                w.run("?DEBUG,ETM,OFF")
            finally:
                w.run("?DEBUG,ETM,OFF")
                if renumbered:
                    w.run("?WCB,1")
                    w.reboot()
        time.sleep(2)
        if not _has(w.run("?WDP,DUMP", timeout=8), "[WDP:N=1,"):
            problems.append("after the restore the WDP self row is not N=1")
        rows2 = {int(m.group(1)) for x in _crun(c2, "?WDP,DUMP", 1.5) for m in [re.match(r"^\[WDP:N=(\d+),", x)] if m}
        if 3 in rows2:
            problems.append("W2 learned a WCB 3 although W1's WDP was off")
            _crun(c2, "?WDP,FORGET,3")
    assert not problems, "; ".join(problems)


@test("ident.channel_reboot", "?WCBCH moves W1 off the mesh channel at the next boot (?WIFI reports the radio channel, a unicast to W2 fails at the MAC layer and never arrives) and back with the original value; 0 and 12 are refused (2 reboots)", needs=["wcb1"])
def channel_reboot(bench):
    """saveMeshChannelToPreferences persists and the boot path applies (WCB_Storage.cpp: a live switch would strand a
    relayed push). W1's WiFi AP follows the mesh channel, so a client of it drops for the off-channel window (~30 s).
    Five channels away, not one: on the bench W2 on channel 1 decoded W1's frames on channel 2 and W1 heard the ACKs
    (run 20260924-005924) - adjacent 20 MHz channels overlap, and at bench range that is enough."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        cur = token(before[1], "?WCBCH,")
        if cur is None:
            raise Skip("W1's chain lacks ?WCBCH")
        orig = int(cur.split(",")[1])
        other = (orig + 4) % 11 + 1          # five channels away (1->6, 6->11, 11->5): an adjacent channel is not off-mesh
        moved = False
        try:
            for bad_ch in (0, 12):
                if not _has(w.run(f"?WCBCH,{bad_ch}"), f"Invalid mesh channel {bad_ch}. Valid range: 1-11."):
                    problems.append(f"?WCBCH,{bad_ch} was not refused")
            out = w.run(f"?WCBCH,{other}")
            moved = True
            if not _has(out, f"Mesh channel saved as {other} — reboot to apply."):
                problems.append(f"?WCBCH,{other} printed {out}")
            if f"?WCBCH,{other}" not in snapshot(bench, 1):
                problems.append("the chain does not hold the new channel")
            w.reboot()
            radio = next((x.rstrip() for x in w.run("?WIFI") if x.startswith("Radio channel : ")), "")
            if not radio.startswith(f"Radio channel : {other}  (mesh channel {other})"):
                problems.append(f"after the reboot the radio is not on channel {other}: {radio!r}")
            w.run("?DEBUG,ETM,ON")
            t = marker()
            watch, wm = Watch(w2s2), w.dev.mark()
            w.send(f";W2,;S2{t}")
            # Nobody on this channel answers the 802.11 frame, so the send callback reports a MAC-layer failure
            # (espNowSendCallback, WCB.ino) and the ETM entry resolves at once: there is no ACK to wait for and
            # no retry line. The deaf-board case (ident.mac_octet2_live) is the one that retries three times.
            try:
                w.dev.expect(r"^\[SEND CB\] MAC-layer FAILED to: [0-9A-F:]+:02$", timeout=5, since=wm)
                w.dev.expect(r"^\[ETM\] Seq \d+ resolved", timeout=5, since=wm)
            except AssertionError:
                problems.append("off-channel, the unicast to W2 was not reported as a MAC-layer failure")
            time.sleep(0.5)
            if t.encode() in watch.got(w2s2):
                problems.append("off-channel, the unicast still reached W2 S2")
            w.run("?DEBUG,ETM,OFF")
        finally:
            w.run("?DEBUG,ETM,OFF")
            if moved:
                w.run(f"?WCBCH,{orig}")
                w.reboot()
        time.sleep(3)
        radio = next((x.rstrip() for x in w.run("?WIFI") if x.startswith("Radio channel : ")), "")
        if not radio.startswith(f"Radio channel : {orig}  (mesh channel {orig})"):
            problems.append(f"after the restore the radio is not on channel {orig}: {radio!r}")
        t = marker()
        watch = Watch(w2s2)
        w.send(f";W2,;S2{t}")
        try:
            watch.expect(w2s2, t.encode(), timeout=5)
        except AssertionError:
            problems.append("back on the mesh channel, a unicast to W2 was not delivered")
    assert not problems, "; ".join(problems)


@test("ident.mac_octet2_live", "?MAC,2 changes the receive filter at once: W1 hears nothing (its unicast to W2 fails, W2's command to it is ignored) until the octet is put back; ?MAC argument errors; the legacy ?M2xx spelling", needs=["wcb1"])
def mac_octet2_live(bench):
    """espNowReceiveCallback drops a frame whose source octets differ from umac_oct2 / umac_oct3 (WCB.ino); the radio
    address itself changes only at boot, so W1 still transmits. Same mechanism as s18's _deaf_w1, on the other octet.
    The octet is written to NVS at once, so it is restored first thing and W1 is never reset in the window."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before, Console(bench, 2) as c2:
        cur = token(before[1], "?MAC,2,")
        if cur is None:
            raise Skip("W1's chain lacks ?MAC,2")
        orig = cur[len("?MAC,2,"):]
        other = "%02X" % (int(orig, 16) ^ 0x01)
        for cmd, want in (("?MAC,2", "Invalid format. Use: ?MAC,2,xx or ?MAC,3,xx"), ("?MAC,4,AA", "Invalid octet. Use 2 or 3")):
            if not _has(w.run(cmd), want):
                problems.append(f"{cmd} did not print {want!r}")
        deaf = False
        try:
            w.run("?DEBUG,ETM,ON")
            out = w.run(f"?MAC,2,{other}")
            deaf = True
            if not _has(out, f"Updated 2nd MAC octet to 0x{other}"):
                problems.append(f"?MAC,2,{other} printed {out}")
            t = marker()
            watch, wm = Watch(w2s2), w.dev.mark()
            w.send(f";W2,;S2{t}")
            try:
                w.dev.expect(rf"\[ETM\] WCB2 failed to ACK seq \d+ after 3 retries: ;S2{t}", timeout=10, since=wm)
            except AssertionError:
                problems.append("deaf on octet 2, the unicast's ACKs were still heard")
            if t.encode() not in watch.got(w2s2):
                problems.append("W2 did not run the unicast (W1 still transmits while deaf)")
            u = marker("u")
            wm = w.dev.mark()
            _crun(c2, f";W1,;S0{u}", 2.0)
            if _has(w.dev.since(wm), u):
                problems.append("deaf on octet 2, W1 still ran a command from W2")
        finally:
            out = w.run(f"?MAC,2,{orig}")
            w.run("?DEBUG,ETM,OFF")
            if deaf and not _has(out, f"Updated 2nd MAC octet to 0x{orig}"):
                problems.append(f"restoring octet 2 printed {out}")
        u = marker("v")
        wm = w.dev.mark()
        _crun(c2, f";W1,;S0{u}", 2.0)
        if not _has(w.dev.since(wm), u):
            problems.append("after the restore W1 did not run a command from W2")
        if not _has(w.run(f"?M2{orig}"), f"Updated the 2nd Octet to 0x{orig}"):        # legacy spelling, same value
            problems.append("legacy ?M2xx did not confirm")
        if not _has(w.run("?M2ZZ"), "Invalid hex value for 2nd MAC octet."):
            problems.append("legacy ?M2ZZ was not refused")
    assert not problems, "; ".join(problems)


@test("ident.hw_setter", "?HW saves a hardware version for the next boot and the chain reports it at once (F3), refuses unknown ones, and the legacy ?HW<n> spelling reaches the same setter; put back without a reboot (the pin map applies at boot)", needs=["wcb1"], links=[])
def hw_setter(bench):
    """saveHWversion (WCB_Storage.cpp) validates before writing, so an invalid value leaves NVS alone - that once
    clobbered a board's pin map from a Wizard push with no hardware selected. Since 2026-09-24 it also updates the
    running value, so the chain (collectConfigCommands emits wcb_hw_version) shows the saved version at once; the pin
    map itself is applied at boot only (HIL_TEST_AUDIT.md F3, tracker #83). On a version-1.0 board like W1 the
    status-LED code reads the running value too, so the LED follows the NeoPixel branch for the second another
    version is set."""
    w = usb_wcb(bench)
    names = {1: "1.0", 21: "2.1", 23: "2.3", 24: "2.4", 31: "3.1", 32: "3.2"}
    problems = []
    with config_guard(bench, 1) as before:
        cur = token(before[1], "?HW,")
        orig = int(cur.split(",")[1]) if cur else 0
        if orig not in names:
            raise Skip(f"W1's chain says {cur}: no known hardware version to put back")
        other = 32 if orig != 32 else 31
        changed = False
        try:
            for bad in (0, 99):
                if not _has(w.run(f"?HW,{bad}"), f"No valid HW version identified ({bad}) — stored HW version unchanged."):
                    problems.append(f"?HW,{bad} was not refused")
            if f"?HW,{orig}" not in snapshot(bench, 1):
                problems.append("a refused ?HW changed the stored version")
            out = w.run(f"?HW,{other}")
            changed = True
            if not _has(out, f"Saved HW Ver: {names[other]} to NVS.  Reboot to take effect!"):
                problems.append(f"?HW,{other} printed {out}")
            if f"?HW,{other}" not in snapshot(bench, 1):
                problems.append("the chain does not report the saved hardware version")
            out = w.run(f"?HW{orig}")                                  # legacy spelling puts it back
            if not _has(out, f"Saved HW Ver: {names[orig]} to NVS.  Reboot to take effect!"):
                problems.append(f"legacy ?HW{orig} printed {out}")
            changed = f"?HW,{orig}" not in snapshot(bench, 1)
        finally:
            if changed:
                w.run(f"?HW,{orig}")
    assert not problems, "; ".join(problems)


# ============================================================ destructive, restorable
@test("bcast.reset_legacy", "?RESET_BROADCAST rebuilds the broadcast namespace with every port open (S1 included, S0 echo off), printing each key; a port turned off beforehand receives broadcasts again; the baseline flags are replayed afterwards", needs=["wcb1"])
def reset_legacy(bench):
    """resetBroadcastSettingsNamespace (WCB_Storage.cpp), the legacy spelling of ?BCAST,RESET. RESET means every port
    comes back open, device-owned ports included (Greg, 2026-09-21), so W1's Maestro port S1 is ON/ON until the baseline
    ?BCAST tokens are replayed - a Maestro port never receives a broadcast anyway (s12 bcast_maestro_port_excluded)."""
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?BCAST,OUT,S3,ON")
    problems = []
    with config_guard(bench, 1) as before:
        flags = [t for t in before[1] if t.upper().startswith("?BCAST,")]
        try:
            w.run("?BCAST,OUT,S3,OFF")
            t = marker()
            watch = Watch(s3)
            w.send(t)
            time.sleep(1.0)
            if t.encode() in watch.got(s3):
                problems.append("setup: a broadcast reached S3 with its output off")
            out = [x.rstrip() for x in w.run("?RESET_BROADCAST")]
            problems += _in_order(out, ["Clearing broadcast_settings namespace...", "Recreating broadcast_settings with defaults..."]
                                  + [f"  Created S{n} = true (result: SUCCESS)" for n in range(1, 6)], "?RESET_BROADCAST")
            toks = snapshot(bench, 1)
            problems += [f"after the reset the chain lacks {t}" for p in range(1, 6)
                         for t in (f"?BCAST,OUT,S{p},ON", f"?BCAST,IN,S{p},ON") if t not in toks]
            if "?BCAST,OUT,S0,OFF" not in toks:
                problems.append("the reset did not leave S0 echo off")
            t = marker()
            watch = Watch(s3)
            w.send(t)
            try:
                watch.expect(s3, t.encode(), timeout=2)
            except AssertionError:
                problems.append("a broadcast did not reach S3 after the reset")
        finally:
            for f in flags:
                w.run(f)
    assert not problems, "; ".join(problems)


@test("wdp.clear_learned_relearn", "?WDP,CLEAR empties the neighbour table and drops every learned peer (?PEERSLIVE falls to the floor, the ids stop being targets); the floor peer W2 stays reachable; POLL relearns W2; ?WDP,ADD puts each learned peer back (auto-join off meanwhile)", needs=["wcb1"])
def clear_learned_relearn(bench):
    """clearAllLearnedPeers + the table memsets (WCB_WDP.cpp `CLEAR`, WCB.ino). Learned peers are not in the config chain
    and nothing lists them by id, so they are enumerated by asking (see _learned_peers) before and after, and put back
    with ?WDP,ADD,<id> (the same learned bit, flushed to NVS 5 s later). Auto-join is off in between so no advert can
    re-learn anything behind the test's back."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        if "?WDP,OFF" in before[1] or "?WDP,AUTOJOIN,OFF" in before[1]:
            raise Skip("W1 has WDP or auto-join off")
        floor = int(token(before[1], "?WCBQ,").split(",")[1])
        learned = _learned_peers(w, floor)
        count = _live_peers(w)
        rows = _dump_rows(w)
        bench.note(f"learned peers before: {learned}; live peers {count}; dump rows {rows}")
        if count != floor - 1 + len(learned):
            problems.append(f"?PEERSLIVE says {count} but the floor ({floor}, minus self) and {len(learned)} learned peers make {floor - 1 + len(learned)}")
        try:
            if not _has(w.run("?WDP,AUTOJOIN,OFF"), "[WDP] auto-join disabled"):
                raise AssertionError("?WDP,AUTOJOIN,OFF did not confirm")
            out = w.run("?WDP,CLEAR")
            if not _has(out, "[WDP] neighbor table + learned peers cleared"):
                problems.append(f"?WDP,CLEAR printed {out}")
            if learned and not _has(out, f"[PEER] dropped {len(learned)} learned peer(s)"):
                problems.append(f"?WDP,CLEAR did not report dropping {len(learned)} learned peer(s): {out}")
            if _live_peers(w) != floor - 1:
                problems.append(f"after the clear ?PEERSLIVE is not the floor alone: {_live_peers(w)}")
            after = _dump_rows(w)
            if [n for n in learned if n in after]:
                problems.append(f"learned rows survived the clear: {after}")
            t = marker()
            watch = Watch(w2s2)
            w.send(f";W2,;S2{t}")
            try:
                watch.expect(w2s2, t.encode(), timeout=3)
            except AssertionError:
                problems.append("the floor peer W2 became unreachable after ?WDP,CLEAR")
            still = _learned_peers(w, floor)
            if still:
                problems.append(f"still targets after the clear: {still}")
            if not _has(w.run("?WDP,POLL"), "[WDP] polled"):
                problems.append("?WDP,POLL did not confirm")
            time.sleep(4)
            relearned = _dump_rows(w)
            if 2 in rows and 2 not in relearned:
                problems.append("POLL did not relearn W2's row")
            bench.note(f"dump rows 4 s after POLL: {relearned}")
            for n in learned:
                out = w.run(f"?WDP,ADD,{n}")
                if not _has(out, f"[WDP] added WCB{n} as a peer"):
                    problems.append(f"re-adding learned peer {n} printed {out}")
            time.sleep(6)                          # LEARNED_FLUSH_DEBOUNCE_MS (5 s) - the NVS write
        finally:
            w.run("?WDP,AUTOJOIN,ON")
            for n in learned:
                w.run(f"?WDP,ADD,{n}")             # idempotent: already a member prints the same line
            w.run("?WDP,POLL")
            time.sleep(6)
        final = _learned_peers(w, floor)
        if final != learned:
            problems.append(f"learned peers after the restore: {final}, before: {learned}")
        if _live_peers(w) != count:
            problems.append(f"?PEERSLIVE after the restore: {_live_peers(w)}, before: {count}")
    assert not problems, "; ".join(problems)


@test("pwm.legacy_pos_pclear", "Legacy ?POS<n> declares a PWM output like ?MAP,PWM,OUT (live, listed by ?PLIST, in the chain) and ?PCLEAR clears everything with the deferred reboot (1 reboot)", needs=["wcb1"], links=[])
def legacy_pos_pclear(bench):
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON")
    problems = []
    with config_guard(bench, 1):
        declared = False
        try:
            out = w.run("?POS4")
            declared = _has(out, "Serial4 configured as PWM output port")
            if not declared:
                problems.append(f"?POS4 printed {out}")
            if "Configured outputs: S4" not in [x.rstrip() for x in w.run("?PLIST")]:
                problems.append("?PLIST does not list the output ?POS4 declared")
            if "?MAP,PWM,OUT,S4" not in snapshot(bench, 1):
                problems.append("the chain lacks ?MAP,PWM,OUT,S4")
            m = w.send("?PCLEAR")
            w.dev.expect(r"^All PWM mappings cleared", timeout=3, since=m)
            _pwm_reboot(w, m)
            declared = False
            if any(t.upper().startswith("?MAP,PWM") for t in snapshot(bench, 1)):
                problems.append("PWM tokens survived ?PCLEAR")
        finally:
            if declared:
                m = w.send("?MAP,PWM,CLEAR,OUT,S4")
                _pwm_reboot(w, m)
    assert not problems, "; ".join(problems)


@test("kyber.legacy_clear_remote", "Legacy ?KYBER_CLEAR and ?KYBER_REMOTE reach storeKyberSettings: CLEAR says a reboot is needed and 'Kyber is Not used', REMOTE says 'Kyber is REMOTE' and lands in broadcast mode; the usual restore reboot follows (WDP off; 1 reboot)", needs=["wcb1"], links=[])
def legacy_clear_remote(bench):
    w = usb_wcb(bench)
    _require_kyber_broadcast_remote(w)
    problems = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            try:
                out = [x.rstrip() for x in w.run("?KYBER_CLEAR", timeout=6)]
                problems += _in_order(out, ["Kyber cleared. Run ?MAESTRO_DEFAULT to clear Maestro configs.", "Reboot required",
                                            "Kyber is Not used"], "?KYBER_CLEAR")
                out = [x.rstrip() for x in w.run("?KYBER_REMOTE", timeout=6)]
                problems += _in_order(out, ["Kyber is REMOTE (on another WCB)", "Reboot required", "Kyber is Remote"], "?KYBER_REMOTE")
                lst = _kyber_list(w)
                if "Kyber is Remote" not in lst or "Targeting mode: Disabled (Broadcast Mode)" not in lst:
                    problems.append(f"after ?KYBER_REMOTE ?KYBER,LIST says {lst}")
            finally:
                _restore_remote(w, [])          # ?KYBER,CLEAR + ?MAESTRO,REMOTE + reboot, Maestro_Remote task checked
    assert not problems, "; ".join(problems)
