"""Identity and radio settings, and the destructive-but-restorable commands (docs/HIL_TEST_AUDIT.md WP3 / WP4).

Everything here changes W1 through its own USB console and puts it back the same way, so a board that is unreachable
over the mesh for a moment is still reachable to the test. Rules:
- W1 is renumbered only to a board number nobody has (suites/common.py absent_wcbs: WCB 3 on a bench of W1 and W2, 4
  once a real WCB3 is listed), and its WDP is off meanwhile, so no board or client can learn that number (W2 would
  persist it; NaviCore would auto-join it): auto-join only ever runs from a WDP advert (WCB_WDP.cpp, addActivePeer), and
  heartbeats persist nothing.
- ?WCBCH and ?WCB apply at boot, ?HW only at boot (the pin map), ?MAC,2 at once (the receive filter). ?HW is set and
  put back WITHOUT a reboot in between: booting W1 on another board's pin map would take its ports away.
- ?EPASS and ?ERASE,NVS are not sent: the first would put the mesh credential in test code, the second needs the
  whole factory chain replayed and loses W1's persisted learned peers and device records. Both stay attended-only
  (HIL_TEST_AUDIT.md §7).
- Restore if aborted, on W1's USB console: ?WCB,1 / ?WCBCH,<chain value> / ?MAC,2,<chain value> / ?MAC,3,<chain value> /
  ?HW,<chain value>, then ?reboot; ?MAESTRO,CLEAR,M8 if ?MAESTRO,LIST shows a Maestro 8 (ident.wcb_number_reboot adds
  one, and W1's WDP must stay off until it is gone); ?WDP,ON; ?WDP,AUTOJOIN,ON; ?WDP,ADD,<n> for each learned peer the
  run log lists (the WDP tests note them before they start). Nothing here moves a servo.
- The coverage re-scan rows (docs/hil_plan/WCB.md WCB-WP16) that are here: row 1 (ident.hw_other_chip_refused, and
  ident.hw_setter on a same-chip version), row 2 (ident.mac_hex_refused), row 3 (wdp.peers.add_forget_survive_quick_reboot;
  its plain-reboot half, the restore count, is boot.banner_w1 in s29 and wdp.autojoin_permanent_downgrade in s18),
  row 4 (wdp.peers.fingerprint_discard), row 5 (wdp.peers.forget_learned_persists), row 6 (ident.sender_id_mac_bound)
  and row 7 (the Maestro self-slot repair inside ident.wcb_number_reboot, which reuses that test's renumbered boot).
"""
import re
import time

from hil.runner import Skip, test
from suites.common import (Console, Watch, absent_wcbs, config_guard, link, marker, require_tokens, snapshot, token,
                           usb_wcb)
from suites.s14_pwm import _no_pwm, _pwm_reboot
from suites.s22_maestro_kyber import _kyber_list, _require_kyber_broadcast_remote, _restore_remote, _wdp_off
from suites.s99_etm import _peers_online


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
REPAIRED_M8 = "[MAESTRO] repaired legacy remote-to-self slot: M8 → local S1"


def _self_slot_setup(tokens, me):
    """(baud, None) for the Maestro self-slot repair in ident.wcb_number_reboot, or (None, why) when that half cannot
    run. It needs a free slot, no Maestro 8, and W1's own local Maestro on S1, whose baud the M8 proxy copies: clearing a
    local slot resets its port to 9600 with broadcasts on unless another local slot still uses the port
    (_clearMaestroSlot, WCB_Maestro.cpp:961-986), so S1's own Maestro is what keeps S1 as it is through the clear."""
    slots = [t for t in tokens if re.match(r"^\?MAESTRO,M\d+:W\d+S\d:\d+$", t, re.I)]
    if any(t.upper().startswith("?MAESTRO,M8:") for t in slots):
        return None, "W1 already holds a Maestro 8"
    if len(slots) >= 9:
        return None, "all nine of W1's Maestro slots are in use"
    own = next((t for t in slots if re.match(rf"^\?MAESTRO,M[1-7]:W{me}S1:\d+$", t, re.I)), None)
    if own is None:
        return None, "W1 has no local Maestro on S1 to keep S1's baud and flags through the clear"
    return int(own.rsplit(":", 1)[1]), None


def _m8_rows(w):
    """?MAESTRO,LIST's rows for Maestro 8 (printMaestroSettings, WCB_Storage.cpp:2632-2664)."""
    return [x.rstrip() for x in w.run("?MAESTRO,LIST") if x.startswith("  Maestro 8 ")]


@test("ident.wcb_number_reboot", "?WCB,<n> renumbers W1 at the next boot, n a board nobody has (3 here): the chain says ?WCB,<n>, the WDP self row and the ETM heartbeats say n, and a Maestro proxy M8 -> W<n> added first comes back from that boot repaired into a local S1 slot; ?WCB,21 is refused; ?MAESTRO,CLEAR,M8, ?WCB,1 and a reboot put it back (WDP off; 2 reboots)", needs=["wcb1"], links=[])
def wcb_number_reboot(bench):
    """saveWCBNumberToPreferences (WCB_Storage.cpp:248-257) takes effect live for the number itself (so a push's later
    MAESTRO lines use it) and at boot for the radio address, peers and ETM.

    The new number is absent_wcbs' first (suites/common.py): 3 on a bench of W1 and W2, 4 once a real WCB3 is listed,
    since W1 booting as a board on the mesh would be a second radio with its MAC and id. The bench precondition that was
    ?WCBQ,2 is now W1's floor below that number (2 below 3 here). Every restore path ends on ?WCB,1 and a reboot.

    The Maestro self-slot repair (WCB-WP16 row 7) rides the same renumbered boot, so it adds no identity window of its
    own. A remote proxy M8 -> W<n>, added while W1 is still WCB 1 (a remote slot stores port 0 and host n,
    WCB_Maestro.cpp:869-879), points at W1 itself once W1 boots as WCB n. setup() runs normalizeMaestroSelfSlots right
    after the number is loaded (WCB.ino:9365, :9384): the slot becomes local on S1 and is saved
    (WCB_Storage.cpp:2618-2631). A local slot is advertised over WDP, so it is cleared while W1 is still WCB n with its
    WDP off, and WDP stays off, failing the test loudly, if it cannot be cleared (docs/HIL_TESTING.md §1). The clear
    leaves S1 alone because W1's own local Maestro still uses the port (_self_slot_setup)."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    if me != 1:
        raise Skip("the console board is not WCB 1")
    new = absent_wcbs(bench)[0]
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        q = token(before[1], "?WCBQ,")
        if q is None or int(q.split(",")[1]) >= new:
            raise Skip(f"W1's {q} floor takes in WCB{new}" if q else "W1's chain has no ?WCBQ")
        if "?WDP,OFF" in before[1]:
            raise Skip("W1's WDP is already off, so this test could not tell its own ?WDP,OFF apart from the bench's")
        rows2 = {int(m.group(1)) for x in _crun(c2, "?WDP,DUMP", 1.5) for m in [re.match(r"^\[WDP:N=(\d+),", x)] if m}
        if new in rows2:
            raise Skip(f"W2 already knows a WCB {new}")
        baud, why = _self_slot_setup(before[1], me)
        if baud is None:
            bench.note(f"Maestro self-slot repair not checked: {why}")
        if not _has(w.run("?WDP,OFF"), "[WDP] disabled"):
            raise AssertionError("?WDP,OFF did not confirm")
        renumbered = m8 = False
        try:
            try:
                if baud is not None:
                    out = w.run(f"?MAESTRO,M8:W{new}S1:{baud}")
                    m8 = True
                    if not _has(out, f"✓ Maestro 8: Remote on WCB{new} (unicast, slot "):
                        problems.append(f"?MAESTRO,M8:W{new}S1:{baud} printed {out}")
                out = w.run("?WCB,21")
                if not _has(out, "Invalid WCB number 21. Valid range: 1-20."):
                    problems.append(f"?WCB,21 printed {out}")
                out = w.run(f"?WCB,{new}")
                renumbered = True
                problems += _in_order(out, [f"Changed WCB Number to: {new}", "Please reboot to take full effect"], f"?WCB,{new}")
                if f"?WCB,{new}" not in snapshot(bench, 1):
                    problems.append(f"the chain does not say ?WCB,{new}")
                bm = w.reboot()
                if m8:
                    if not _has(w.dev.since(bm), REPAIRED_M8):
                        problems.append(f"the WCB {new} boot did not print {REPAIRED_M8!r}")
                    # LIST tells the two apart; the chain cannot: while W1 is WCB n a local S1 slot and a proxy to W<n>
                    # both emit M8:W<n>S1:<baud> (emitMaestroBackup, WCB_Maestro.cpp:1145-1162).
                    rows = _m8_rows(w)
                    if rows != ["  Maestro 8 → Local S1"]:
                        problems.append(f"after the WCB {new} boot ?MAESTRO,LIST lists Maestro 8 as {rows}, not one local S1 slot")
                if not _has(w.run("?WDP,DUMP", timeout=8), f"[WDP:N={new},"):
                    problems.append(f"after the reboot the WDP self row is not N={new}")
                w.run("?DEBUG,ETM,ON")
                m = w.dev.mark()
                try:
                    w.dev.expect(rf"^\[ETM\] Heartbeat sent \(WCB{new}\)", timeout=30, since=m)
                except AssertionError:
                    problems.append(f"no ETM heartbeat as WCB{new} within 30 s of the reboot")
                w.run("?DEBUG,ETM,OFF")
            finally:
                w.run("?DEBUG,ETM,OFF")
                if m8:
                    try:
                        out = w.run("?MAESTRO,CLEAR,M8")
                        if _has(out, "Re-enabled broadcast") or _has(out, "Reset S1 baud rate"):
                            problems.append(f"?MAESTRO,CLEAR,M8 reset S1 under W1's own Maestro: {out}")
                        m8 = bool(_m8_rows(w))
                    except AssertionError as e:
                        problems.append(f"clearing Maestro 8 failed: {(str(e).splitlines() or [repr(e)])[0]}")
                if renumbered:
                    w.run("?WCB,1")
                    w.reboot()
        finally:
            if m8:
                try:
                    m8 = bool(_m8_rows(w))          # a clear that timed out may still have landed
                except AssertionError:
                    pass
            if m8:
                problems.append(f"W1's WDP is left OFF: its Maestro 8 (a local S1 slot once a WCB {new} boot repaired it) could "
                                "not be cleared, and WDP would advertise it. On W1's USB console type ?MAESTRO,CLEAR,M8, "
                                "check ?MAESTRO,LIST, then ?WDP,ON")
            else:
                w.run("?WDP,ON")
        time.sleep(2)
        if not _has(w.run("?WDP,DUMP", timeout=8), "[WDP:N=1,"):
            problems.append("after the restore the WDP self row is not N=1")
        rows2 = {int(m.group(1)) for x in _crun(c2, "?WDP,DUMP", 1.5) for m in [re.match(r"^\[WDP:N=(\d+),", x)] if m}
        if new in rows2:
            problems.append(f"W2 learned a WCB {new} although W1's WDP was off")
            _crun(c2, f"?WDP,FORGET,{new}")
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
        same_chip = (31, 32) if orig in (31, 32) else (24, 23, 21, 1)   # the other chip's are refused
        other = next(v for v in same_chip if v != orig)                  # (ident.hw_other_chip_refused)
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


@test("ident.hw_other_chip_refused", "?HW refuses a hardware version of the other chip family (3.1/3.2 are ESP32-S3 boards, the rest classic ESP32): the stored and running version stay put; no reboot (re-scan #2)", needs=["wcb1"], links=[])
def hw_other_chip_refused(bench):
    """WCB coverage re-scan #2 (docs/hil_plan/WCB.md WCB-WP16). The other chip's pin map puts serial ports on SPI-flash
    pins - HW 3.2 on a classic ESP32 gives S2 GPIO6/7 and S5 GPIO9/10 - so the board boot-loops at its next restart
    until a USB reflash, and one wrong ?HW in a Wizard push was enough. saveHWversion (WCB_Storage.cpp) now refuses it
    (hwVersionFitsChip) and loadHWversion ignores one saved by older firmware. The chain reports the running value
    (F3), so it shows a refused version never took. Nothing reboots here: should the refusal ever regress, the version
    is put back at once, before anything restarts W1."""
    w = usb_wcb(bench)
    classic, s3 = (1, 21, 23, 24), (31, 32)
    with config_guard(bench, 1) as before:
        cur = token(before[1], "?HW,")
        orig = int(cur.split(",")[1]) if cur else 0
        if orig in classic:
            others, says = s3, "is an ESP32-S3 board; this is a classic ESP32"
        elif orig in s3:
            others, says = classic, "is a classic-ESP32 board; this is an ESP32-S3"
        else:
            raise Skip(f"W1's chain says {cur}: no known hardware version to keep")
        problems, took = [], False
        try:
            for v in others:
                out = w.run(f"?HW,{v}")
                if not _has(out, f"HW version {v} {says} — stored HW version unchanged."):
                    problems.append(f"?HW,{v} printed {out}")
                if f"?HW,{orig}" not in snapshot(bench, 1):
                    took = True
                    problems.append(f"?HW,{v} changed the version W1 reports")
                    w.run(f"?HW,{orig}")
        finally:
            if took:
                w.run(f"?HW,{orig}")
    assert not problems, "; ".join(problems)


@test("ident.mac_hex_refused", "?MAC,2|3 refuses a value that is not one or two hex digits (ZZ, empty, 1FF, 0x) as the legacy ?M2/?M3 do: the chain keeps W1's octets and W2 still acknowledges W1 (re-scan #5)", needs=["wcb1"], links=[])
def mac_hex_refused(bench):
    """WCB coverage re-scan #5 (docs/hil_plan/WCB.md WCB-WP16 row 2). The ?MAC,2|3 handler (WCB.ino) ran strtoul with
    no end check, so 'ZZ' or an empty value saved 0x00 and '1FF' saved 0xFF. The octets are the receive filter and
    apply at once, so the board went deaf to its mesh group until someone typed the right value on its USB. The
    finally replays W1's own ?MAC tokens should the refusal ever regress."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        macs = [x for x in before[1] if x.upper().startswith("?MAC,")]
        if len(macs) != 2:
            raise Skip(f"W1's chain has {macs}, not one token per octet")
        try:
            for line, which in (("?MAC,2,ZZ", "2nd"), ("?MAC,2,", "2nd"), ("?MAC,3,1FF", "3rd"), ("?MAC,3,0x", "3rd")):
                out = w.run(line)
                if not _has(out, f"Invalid hex value for {which} MAC octet. Use two hex digits (00-FF)."):
                    problems.append(f"{line} printed {out}")
            now = [x for x in snapshot(bench, 1) if x.upper().startswith("?MAC,")]
            if now != macs:
                problems.append(f"the chain's MAC tokens changed: {now}, before {macs}")
            w.run("?DEBUG,ETM,ON")
            m = w.send(";W2,?VERSION")
            try:
                seq = w.dev.expect(r"^\[ETM\] Sent seq (\d+): \?VERSION", timeout=3, since=m).group(1)
                w.dev.expect(rf"^\[ETM\] Seq {seq} fully acknowledged", timeout=5, since=m)
            except AssertionError:
                problems.append("W2 no longer acknowledges W1 after the refused ?MAC lines")
        finally:
            w.run("?DEBUG,ETM,OFF")
            if [x for x in snapshot(bench, 1) if x.upper().startswith("?MAC,")] != macs:
                for x in macs:
                    w.run(x)
    assert not problems, "; ".join(problems)


@test("ident.sender_id_mac_bound", "An ETM packet whose sender id is not its source MAC's last octet is dropped before anything reads it: W1 renumbered live to a board nobody has (3 here; its radio address still ends .01) has its unicast to W2 dropped as an id-spoof, never ACKed or run, and W2 learns no such board; ?WCB,1 goes back at once (no reboot; W1 is renumbered for about 3 s, its WDP off)", needs=["wcb1", "wcb2"])
def sender_id_mac_bound(bench):
    """WCB-WP16 row 6. The ETM receive path checks the claimed sender against the source MAC's last octet before
    presence, ACKs, WDP or commands see the packet (espNowReceiveCallback, WCB.ino:5258-5271), and says so under
    ?DEBUG,ETM: '[ETM] Dropped id-spoof: senderWCB=<n> but src MAC .<octet>'. ?WCB sets WCB_Number live
    (WCB_Storage.cpp:248-257) while the radio address stays the one setup() gave it (WCB.ino:9595), so every packet W1
    sends in the window claims the new number from ...:01, and no probe verb is needed (the other sender guards do need
    one: docs/hil_plan/WCB.md WCB-WP59). ?WCB also writes NVS, so ?WCB,1 goes back first in the finally and W1 is never
    reset in the window. W1's WDP is off, so it sends no advert as that number: NaviCore's WCB_Client has the same gate
    (WCB_Client.cpp:2701-2705), but a learn there would persist, and heartbeats persist nothing on either side. The
    number is absent_wcbs' first, 3 on a bench of W1 and W2: a real board's id would make W2's id-spoof lines and its
    WCB<n> lines ambiguous between W1 and that board."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    if bench.usb_wcb_number() != 1:
        raise Skip("the console board is not WCB 1")
    new = absent_wcbs(bench)[0]
    spoof = f"[ETM] Dropped id-spoof: senderWCB={new} but src MAC .1"
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its ETM debug lines cannot be read")
        if token(before[1], "?WCB,") != "?WCB,1":
            raise Skip(f"W1's chain says {token(before[1], '?WCB,')}, not ?WCB,1")
        rows2 = {int(m.group(1)) for x in _crun(c2, "?WDP,DUMP", 1.5) for m in [re.match(r"^\[WDP:N=(\d+),", x)] if m}
        if new in rows2:
            raise Skip(f"W2 already knows a WCB {new}")
        if "?WDP,OFF" in before[1]:
            raise Skip("W1's WDP is already off, so this test could not tell its own ?WDP,OFF apart from the bench's")
        t = marker()
        if not _has(w.run("?WDP,OFF"), "[WDP] disabled"):
            raise AssertionError("?WDP,OFF did not confirm")
        renumbered, back = False, True
        cm = c2.mark()
        try:
            if not _has(_crun(c2, "?DEBUG,ETM,ON"), "ETM debugging enabled"):
                problems.append("W2's ?DEBUG,ETM,ON did not confirm")
            w.run("?DEBUG,ETM,ON")
            watch = Watch(w2s2)
            out = w.run(f"?WCB,{new}")
            renumbered = True
            problems += _in_order(out, [f"Changed WCB Number to: {new}", "Please reboot to take full effect"], f"?WCB,{new}")
            wm = w.send(f";W2,;S2{t}")
            try:
                w.dev.expect(rf"\[ETM\] WCB2 failed to ACK seq \d+ after 3 retries: ;S2{t}", timeout=10, since=wm)
            except AssertionError:
                problems.append(f"W1's unicast as WCB {new} was not reported unacknowledged after its 3 retries")
        finally:
            if renumbered:
                back = _has(w.run("?WCB,1"), "Changed WCB Number to: 1")
            w.run("?DEBUG,ETM,OFF")
            time.sleep(0.5)
            seen = [x.rstrip() for x in c2.lines(cm)]
            _crun(c2, "?DEBUG,ETM,OFF")
            if back:
                w.run("?WDP,ON")
            else:                                  # WDP stays off: W1 must not advertise while it may still say <new>
                problems.append(f"RESTORE NOT CONFIRMED: W1 may still be WCB {new}, and its WDP is left OFF. On W1's USB "
                                "console type ?WCB,1, check that ?backup says ?WCB,1, then ?WDP,ON - before anything "
                                "reboots W1")
        drops = [x for x in seen if x.startswith(spoof)]
        bench.note(f"W2 dropped {len(drops)} packet(s) from W1 as id-spoofs while W1 claimed WCB {new}")
        if not drops:
            problems.append(f"W2 printed no {spoof!r} while W1 claimed to be WCB {new}")
        if t.encode() in watch.got(w2s2):
            problems.append(f"W2 ran the command W1 sent as WCB {new}")
        acted = [x for x in seen if re.search(rf"\bWCB{new}\b", x) and not x.startswith(spoof)]
        if acted:
            problems.append(f"W2 acted on a packet claiming WCB {new}: {acted[:3]}")
        rows2 = {int(m.group(1)) for x in _crun(c2, "?WDP,DUMP", 1.5) for m in [re.match(r"^\[WDP:N=(\d+),", x)] if m}
        if new in rows2:
            problems.append(f"W2 learned a WCB {new}")
            _crun(c2, f"?WDP,FORGET,{new}")
        t2 = marker()
        watch = Watch(w2s2)
        w.send(f";W2,;S2{t2}")
        try:
            watch.expect(w2s2, t2.encode(), timeout=5)
        except AssertionError:
            problems.append("after ?WCB,1 a unicast from W1 did not reach W2 S2")
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


@test("wdp.peers.add_forget_survive_quick_reboot", "A learned-peer change followed at once by ?reboot is kept: ?WDP,ADD,<id>^?reboot comes back with the id learned, ?WDP,FORGET,<id>^?reboot without it (re-scan #6; 2 reboots)", needs=["wcb1"], links=[])
def add_forget_survive_quick_reboot(bench):
    """WCB coverage re-scan #6 (WCB-WP16 row 3). Learned-peer membership is written to NVS 5 s after a change
    (LEARNED_FLUSH_DEBOUNCE_MS), and a deferred restart fires once the queue has been quiet 4 s, without flushing, so a
    change and a ?reboot in one line were lost at the boot. loop() now flushes a pending change just before the restart.
    The id is one nothing uses: ADD then FORGET first proves W1 accepts it (the controller id is refused), and the 6 s
    after lets that pair's own flush land before the timed lines. While it is learned, W1's ETM broadcasts wait on it."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        if "?WDP,OFF" in before[1]:
            raise Skip("W1 has WDP off")
        floor = int(token(before[1], "?WCBQ,").split(",")[1])
        learned = _learned_peers(w, floor)
        rows = _dump_rows(w)
        k = None
        for n in range(18, floor, -1):
            if n in learned or n in rows:
                continue
            if _has(w.run(f"?WDP,ADD,{n}"), f"[WDP] added WCB{n} as a peer"):
                w.run(f"?WDP,FORGET,{n}")
                k = n
                break
        if k is None:
            raise Skip(f"no unused id above the floor {floor} that ?WDP,ADD accepts")
        time.sleep(6)                                  # the ADD/FORGET pair's own flush
        added = False
        try:
            m = w.send(f"?WDP,ADD,{k}^?reboot")
            added = True
            w.dev.expect(r"^Rebooting now", timeout=w.REBOOT_DEFER_S, since=m)
            w.wait_boot(m, timeout=30)
            if k not in _learned_peers(w, floor):
                problems.append(f"WCB{k}, added in the line that rebooted W1, was not learned after the boot")
            m = w.send(f"?WDP,FORGET,{k}^?reboot")
            w.dev.expect(r"^Rebooting now", timeout=w.REBOOT_DEFER_S, since=m)
            w.wait_boot(m, timeout=30)
            if k in _learned_peers(w, floor):
                problems.append(f"WCB{k}, forgotten in the line that rebooted W1, was learned again after the boot")
            else:
                added = False
        finally:
            if added:
                w.run(f"?WDP,FORGET,{k}")
                time.sleep(6)
        final = _learned_peers(w, floor)
        if final != learned:
            problems.append(f"learned peers after the test: {final}, before: {learned}")
    assert not problems, "; ".join(problems)


def _boot_lines(dev, mark):
    """One boot's own lines after `mark`, through setup()'s last line, 'Raw Serial Forwarding Task Created'
    (WCB.ino:9748). A line naming a password is dropped (setup() prints the mesh password), so none reaches a message."""
    lines = [x.rstrip() for x in dev.since(mark) if "password" not in x.lower()]
    end = next((i for i, x in enumerate(lines) if x == "Raw Serial Forwarding Task Created"), len(lines) - 1)
    return lines[:end + 1]


def _readd(w, ids, problems):
    """?WDP,ADD each id (the learned bit and a flush 5 s later, WCB_WDP.cpp:1896-1905), then wait out the flush."""
    for n in ids:
        if not _has(w.run(f"?WDP,ADD,{n}"), f"[WDP] added WCB{n} as a peer"):
            problems.append(f"?WDP,ADD,{n} did not confirm")
    if ids:
        time.sleep(6)                                  # LEARNED_FLUSH_DEBOUNCE_MS (5 s) - the NVS write


@test("wdp.peers.forget_learned_persists", "?WDP,FORGET on a learned peer unregisters it at once ('[PEER] WCB<n> unregistered.', '[WDP] forgot WCB<n>'), makes it no target and drops ?PEERSLIVE by one; after the 5 s flush a reboot restores the other learned peers and not it; ?WDP,ADD puts it back (auto-join off; 1 reboot)", needs=["wcb1"], links=[])
def forget_learned_persists(bench):
    """WCB-WP16 row 5. ?WDP,FORGET (WCB_WDP.cpp:1907-1916) runs removeActivePeer, which clears the learned bit, schedules
    the NVS flush LEARNED_FLUSH_DEBOUNCE_MS (5 s) later and deletes the out-of-band ESP-NOW peer with '[PEER] WCB<n>
    unregistered.' (WCB.ino:9110-9138), then drops the WDP row (wdpForgetNeighbor). drainLearnedPeerMaintenance writes
    the table once the 5 s have passed (WCB.ino:9216-9221), and at boot loadLearnedPeers registers the rest and prints
    '[PEER] restored <n> learned peer(s) from NVS' (:9187-9213). This is the ordinary path; a FORGET and a ?reboot in one
    line, flushed just before the restart, is wdp.peers.add_forget_survive_quick_reboot. Auto-join is off throughout, so
    an advert from the forgotten board cannot learn it again before the reboot; ?WDP,ADD puts it back afterwards."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        if "?WDP,OFF" in before[1] or "?WDP,AUTOJOIN,OFF" in before[1]:
            raise Skip("W1 has WDP or auto-join off")
        floor = int(token(before[1], "?WCBQ,").split(",")[1])
        temporary = [n for n, peer in _dump_rows(w).items() if peer == 4]
        if temporary:           # a member too, but never persisted: a ?WDP,ADD would make it permanent
            raise Skip(f"WCB{temporary} are temporary peers: _learned_peers cannot tell them from learned ones")
        learned = _learned_peers(w, floor)
        if not learned:
            raise Skip("W1 has no learned peer to forget")
        # an absent one (6 on this bench) before a bench board W1 learned from its adverts (a real WCB3 above the floor)
        k = next((x for x in learned if x not in bench.wcb_numbers()), learned[0])
        rest = [x for x in learned if x != k]
        count = _live_peers(w)
        bench.note(f"learned peers before: {learned}; forgetting WCB{k}; live peers {count}")
        forgot = False
        try:
            if not _has(w.run("?WDP,AUTOJOIN,OFF"), "[WDP] auto-join disabled"):
                raise AssertionError("?WDP,AUTOJOIN,OFF did not confirm")
            out = [x.rstrip() for x in w.run(f"?WDP,FORGET,{k}")]
            forgot = True
            problems += _in_order(out, [f"[PEER] WCB{k} unregistered.", f"[WDP] forgot WCB{k}"], f"?WDP,FORGET,{k}")
            if not _has(w.run(f";W{k},?PEERSLIVE"), f"WCB {k} is not a reachable target"):
                problems.append(f"WCB{k} is still a target after the forget")
            if _live_peers(w) != count - 1:
                problems.append(f"?PEERSLIVE after the forget: {_live_peers(w)}, before: {count}")
            if k in _dump_rows(w):
                problems.append(f"W1's WDP row for WCB{k} survived the forget")
            time.sleep(6)                              # LEARNED_FLUSH_DEBOUNCE_MS (5 s) - the NVS write
            bm = w.reboot()
            boot = _boot_lines(w.dev, bm)
            restored = [x for x in boot if x.startswith("[PEER] restored ")]
            want = [f"[PEER] restored {len(rest)} learned peer(s) from NVS"] if rest else []
            if restored != want:
                problems.append(f"the boot printed {restored}, expected {want}")
            if f"[PEER] WCB{k} registered (live, learned)." in boot:
                problems.append(f"the boot registered the forgotten WCB{k} again")
            now = _learned_peers(w, floor)
            if now != rest:
                problems.append(f"learned peers after the reboot: {now}, expected {rest}")
        finally:
            if forgot:
                _readd(w, [k], problems)
            w.run("?WDP,AUTOJOIN,ON")
            _peers_online(bench, w, strict=False)
        final = _learned_peers(w, floor)
        if final != learned:
            problems.append(f"learned peers after the restore: {final}, before: {learned} - on W1's USB console type "
                            + " ".join(f"?WDP,ADD,{n}" for n in learned if n not in final))
        if _live_peers(w) != count:
            problems.append(f"?PEERSLIVE after the restore: {_live_peers(w)}, before: {count}")
    assert not problems, "; ".join(problems)


@test("wdp.peers.fingerprint_discard", "A learned-peer table saved under other MAC octets is discarded at boot: with ?MAC,3 flipped for one boot W1 prints the discard line, restores nothing and has no learned peer; the octet, a second reboot and ?WDP,ADD put W1 back on the mesh with its learned peers (W1 off its mesh group for ~20 s; 2 reboots)", needs=["wcb1"], links=[])
def fingerprint_discard(bench):
    """WCB-WP16 row 4. saveLearnedPeers stores the membership mask with the octets it was saved under
    (WCB.ino:9169-9180); loadLearnedPeers drops a table whose octets differ from the running ones and clears the
    namespace (:9187-9201). ?MAC,3 writes the octet and the receive filter at once but the radio address only at boot
    (WCB.ino:6485-6520, :9580-9595), so W1 is off its mesh group from the ?MAC,3 until the restore boot: both go on its
    own USB, and the octet goes back first in the finally, as ident.mac_octet2_live does. Nothing may mark the table
    dirty in between, or the pre-restart flush (WCB.ino:9859-9863) would save it again under the new octet: W1 hears
    nothing while its filter is changed, and a temporary peer, which the TTL reaper could evict meanwhile, makes the test
    skip. The discard cleared the namespace, so the restore boot restores nothing and the learned peers are put back with
    ?WDP,ADD. WDP stays on: another group's octets drop W1's adverts at every WCB and WCB_Client (WCB.ino:5097)."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    problems = []
    with config_guard(bench, 1) as before:
        if "?WDP,OFF" in before[1]:
            raise Skip("W1 has WDP off")
        o2, o3 = (token(before[1], f"?MAC,{i},") for i in (2, 3))
        if not o2 or not o3:
            raise Skip("W1's chain lacks its ?MAC tokens")
        o2, o3 = o2.split(",")[2].upper(), o3.split(",")[2].upper()
        floor = int(token(before[1], "?WCBQ,").split(",")[1])
        learned = _learned_peers(w, floor)
        if not learned:
            raise Skip("W1 has no learned peers, so there is no saved table to discard")
        temporary = [n for n, peer in _dump_rows(w).items() if peer == 4]
        if temporary:
            raise Skip(f"WCB{temporary} are temporary peers: an eviction during the window would save the table again")
        count = _live_peers(w)
        bench.note(f"learned peers before: {learned}; live peers {count}; W1's 3rd MAC octet is 0x{o3}")
        other = "%02X" % (int(o3, 16) ^ 0x01)
        flipped = back = False
        restore = (f"on W1's USB console type ?MAC,3,{o3} then ?reboot, then "
                   + " ".join(f"?WDP,ADD,{n}" for n in learned))
        try:
            try:
                flipped = True                         # before the send: one that timed out may still have landed
                out = w.run(f"?MAC,3,{other}")
                if not _has(out, f"Updated 3rd MAC octet to 0x{other}"):
                    problems.append(f"?MAC,3,{other} printed {out}")
                bm = w.reboot()
                boot = _boot_lines(w.dev, bm)
                if f"ESP-NOW MAC Address: 02:{o2}:{other}:00:00:{me:02X}" not in boot:
                    problems.append("the flipped boot did not take the new octet into its radio address")
                if "[PEER] learned-peer table saved under different MAC octets — discarding" not in boot:
                    problems.append("the flipped boot did not discard the learned-peer table")
                if [x for x in boot if x.startswith("[PEER] restored ")]:
                    problems.append("the flipped boot restored learned peers")
                if _live_peers(w) != floor - 1:
                    problems.append(f"after the flipped boot ?PEERSLIVE is {_live_peers(w)}, not the floor alone")
                still = [n for n in learned if not _has(w.run(f";W{n},?PEERSLIVE"), f"WCB {n} is not a reachable target")]
                if still:
                    problems.append(f"after the flipped boot WCB{still} are still targets")
            finally:
                if flipped:
                    try:
                        if not _has(w.run(f"?MAC,3,{o3}"), f"Updated 3rd MAC octet to 0x{o3}"):
                            raise AssertionError("the octet was not confirmed")
                        bm = w.reboot()
                        boot = _boot_lines(w.dev, bm)
                        back = f"ESP-NOW MAC Address: 02:{o2}:{o3}:00:00:{me:02X}" in boot
                        if not back:
                            raise AssertionError("the restore boot did not come up with W1's own octet")
                        if [x for x in boot if x.startswith("[PEER] ")]:
                            problems.append(f"the restore boot still found a learned-peer table: "
                                            f"{[x for x in boot if x.startswith('[PEER] ')]}")
                    except AssertionError as e:
                        problems.append(f"RESTORE NOT CONFIRMED ({(str(e).splitlines() or [repr(e)])[0]}): {restore}")
        finally:
            if back:                                   # the discard cleared the table: put the learned peers back
                _readd(w, learned, problems)
                _peers_online(bench, w, strict=False)
        if back:
            w.run("?DEBUG,ETM,ON")
            m = w.send(";W2,?VERSION")
            try:
                seq = w.dev.expect(r"^\[ETM\] Sent seq (\d+): \?VERSION", timeout=3, since=m).group(1)
                w.dev.expect(rf"^\[ETM\] Seq {seq} fully acknowledged", timeout=5, since=m)
            except AssertionError:
                problems.append("back on its own octet, W1's unicast to W2 was not acknowledged")
            finally:
                w.run("?DEBUG,ETM,OFF")
            final = _learned_peers(w, floor)
            if final != learned:
                problems.append(f"learned peers after the restore: {final}, before: {learned} - on W1's USB console type "
                                + " ".join(f"?WDP,ADD,{n}" for n in learned if n not in final))
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
