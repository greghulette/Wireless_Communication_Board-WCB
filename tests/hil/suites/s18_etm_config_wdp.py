"""ETM settings and delivery, ?STATS, peers and the controller, aliases, the command characters, WDP.

Built from the verified etm_config_wdp specs; console literals re-checked with grep -a (WCB.ino holds a NUL byte, so
ripgrep silently skips it). Rules from the specs:
- Only W1's ETM settings are changed. ETM, CHKSM, MAC, EPASS or WCBCH changes on mesh-only-reachable W2 strand it; the
  one exception, etm.off_fleet_normal_path, switches W2's ETM on W2's own USB and skips when W2 has none.
- '?MAC,3,<other>' deafens W1 at once (its receive filter changes, its radio address only at boot) and writes NVS:
  it is restored in a finally, as the first command, and W1 is never reset in that window.
- An ETM ACK does not mean execution (WCB.ino:4269-4272): delivery is asserted on the probe wire.
- WCB.run() breaks while CMDCHAR is not ';', the delimiter is ',', or the LFI is ';'; those windows use dev.send.
- Probe mesh ids are temporary and never 1/2/19/20 (probe_in_mesh); a temporary peer is evicted 50 s after it
  goes silent (WCB.ino:508), and until then every broadcast expects its ACK, so those tests run last.
- The coverage re-scan tests at the end (docs/hil_plan/WCB.md WP19, WP30, WP35, WP48) never change ?MAC, ?EPASS, ?WCB
  or ?WCBCH. Where the plan deafens W1 (_deaf_w1) to lose a controller's ACK, a probe stands in as the controller
  (?CONTROLLER,ON,15) and MESH LEAVE silences it. A probe joined PERMANENTLY (ids 12 and 13) is learned and persisted
  by every WCB and by NaviCore, so those tests forget it everywhere in their finally (_forget_everywhere).
"""
import re
import time
import zlib
from contextlib import contextmanager

from hil.navicore import NaviCore
from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import (FORBIDDEN_MESH_IDS, Console, Watch, config_guard, link, marker, mesh_params, nonce, padded,
                           probe_in_mesh, remote_wcbs, require_tokens, snapshot, token, usb_wcb)

ETM_KEYS = ("TIMEOUT", "HB", "MISS", "BOOT", "COUNT", "DELAY")


def _has(lines, text):
    return any(text in x for x in lines)


def _etm(tokens):
    """{'TIMEOUT': '500', ...} from a config chain; Skip when any is missing."""
    out = {}
    for k in ETM_KEYS + ("CHKSM",):
        t = token(tokens, f"?ETM,{k},")
        if t is None:
            raise Skip(f"config chain lacks ?ETM,{k}")
        out[k] = t[len(f"?ETM,{k},"):]
    return out


def _restore_etm(w, orig, keys=ETM_KEYS):
    for k in keys:
        w.run(f"?ETM,{k},{orig[k]}")


def _cfg(w):
    return [x.rstrip() for x in w.run("?config")]


# ============================================================ ETM settings
@test("etm.settings_roundtrip", "?ETM TIMEOUT/HB/MISS/BOOT/COUNT/DELAY set, show in ?config and in the chain in order, and restore; TIMEOUT also on W2", needs=["wcb1"], links=[])
def settings_roundtrip(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1, 2) as before:
        orig, orig2 = _etm(before[1]), _etm(before[2])
        try:
            for cmd, want in (("?ETM,TIMEOUT,750", "ETM timeout set to 750 ms"), ("?ETM,HB,15", "ETM heartbeat set to 15 sec"),
                              ("?ETM,MISS,7", "ETM missed heartbeats set to 7"), ("?ETM,BOOT,3", "ETM boot window set to 3 sec"),
                              ("?ETM,COUNT,30", "ETM char message count set to 30"), ("?ETM,DELAY,150", "ETM char delay set to 150 ms")):
                if not _has(w.run(cmd), want):
                    bad.append(f"{cmd} did not print {want!r}")
            cfg = _cfg(w)
            bad += [f"?config lacks {x!r}" for x in ("Heartbeat interval:   15 sec (+/- 1)", "Boot heartbeat:       1-3 sec",
                                                     "Offline after:        7 missed heartbeats (112 sec max)", "Retry timeout:        750 ms",
                                                     "Char message count:   30  delay: 150 ms") if not _has(cfg, x)]
            tokens = snapshot(bench, 1)
            want = ["?ETM,ON", "?ETM,TIMEOUT,750", "?ETM,HB,15", "?ETM,MISS,7", "?ETM,BOOT,3", "?ETM,COUNT,30", "?ETM,DELAY,150",
                    f"?ETM,CHKSM,{orig['CHKSM']}"]
            i = tokens.index("?ETM,ON") if "?ETM,ON" in tokens else -1
            if tokens[i:i + len(want)] != want:
                bad.append(f"chain ETM tokens {tokens[i:i + len(want)] if i >= 0 else 'no ?ETM,ON'}")
            w.send(";W2,?ETM,TIMEOUT,750")      # only TIMEOUT on W2: never HB/MISS/CHKSM there
            time.sleep(1.5)
            if "?ETM,TIMEOUT,750" not in snapshot(bench, 2):
                bad.append("W2's chain lacks ?ETM,TIMEOUT,750")
        finally:
            _restore_etm(w, orig)
            w.send(f";W2,?ETM,TIMEOUT,{orig2['TIMEOUT']}")
            time.sleep(1.5)
    assert not bad, "; ".join(bad)


@test("etm.settings_persist_reboot", "ETM settings survive a W1 reboot (NVS etm_config) (1 reboot)", needs=["wcb1"], links=[])
def settings_persist_reboot(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        try:
            set_ok = _has(w.run("?ETM,HB,12"), "ETM heartbeat set to 12 sec") and _has(w.run("?ETM,TIMEOUT,650"), "ETM timeout set to 650 ms")
            m = w.reboot()
            boot = w.dev.since(m)
            hb, cfg = w.run("?ETM,HB"), _cfg(w)
        finally:
            _restore_etm(w, orig, ("HB", "TIMEOUT"))
    assert set_ok, "the setters did not confirm"
    assert _has(boot, "ETM: ENABLED"), "the boot banner lacks 'ETM: ENABLED'"
    assert _has(hb, "ETM heartbeat is 12 sec"), f"?ETM,HB after reboot: {hb}"
    assert _has(cfg, "Heartbeat interval:   12 sec (+/- 1)") and _has(cfg, "Retry timeout:        650 ms"), "?config after reboot"


@test("etm.query_and_validation", "?ETM,HB / ?ETM,MISS query forms, range rejection, bad subcommands; nothing changes", needs=["wcb1"], links=[])
def query_and_validation(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        if "?ETM,ON" not in before[1]:
            raise Skip("ETM is off on W1")
        orig = _etm(before[1])
        hb = "Invalid ETM heartbeat '{}'. Use 1-3600 seconds."
        miss = "Invalid ETM missed-heartbeat count '{}'. Use 1-100."
        checks = [("?ETM,HB", f"ETM heartbeat is {orig['HB']} sec"), ("?ETM,MISS", f"ETM missed heartbeats is {orig['MISS']}"),
                  ("?ETM,HB,0", hb.format("0")), ("?ETM,HB,3601", hb.format("3601")), ("?ETM,HB,abc", hb.format("abc")),
                  ("?ETM,MISS,0", miss.format("0")), ("?ETM,MISS,101", miss.format("101")),
                  ("?ETM", "Invalid ETM command. Use: ?ETM ?"), ("?ETM,BOGUS", "Invalid ETM command. Use: ?ETM ?"),
                  ("?ETM,CHKSM", "Use: ?ETM,CHKSM,ON or ?ETM,CHKSM,OFF"), ("?ETM,CHKSM,MAYBE", "Use: ?ETM,CHKSM,ON or ?ETM,CHKSM,OFF"),
                  ("?ETM,on", "ETM enabled")]
        bad = [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
    assert not bad, "; ".join(bad)


@test("etm.unvalidated_setters", "(should) A bare ?ETM,TIMEOUT / BOOT / DELAY does not persist 0, and a negative timeout is rejected", needs=["wcb1"], links=[])
def unvalidated_setters(bench):
    """Probable bug: ?ETM,TIMEOUT, ?ETM,BOOT and ?ETM,DELAY (and legacy ?ETMTIMEOUT/?ETMBOOT/?ETMCHARDELAY, WCB.ino:
    5888-5901) have no query form and no range check (WCB.ino:5209-5212, 5243-5254). Asking with the bare command
    persists 0 — the trap WCB.ino:5214-5217 fixes for HB and MISS only — and a negative TIMEOUT makes pending entries
    never expire (WCB.ino:1353). Recorded, restored at once, then asserted."""
    w = usb_wcb(bench)
    out = {}
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        try:
            for cmd in ("?ETM,TIMEOUT", "?ETM,TIMEOUT,-5", "?ETM,BOOT", "?ETM,DELAY", "?ETM,COUNT", "?ETM,COUNT,500"):
                out[cmd] = [x.rstrip() for x in w.run(cmd)]
        finally:
            _restore_etm(w, orig, ("TIMEOUT", "BOOT", "DELAY", "COUNT"))
    bench.note("unvalidated ETM setters: " + " | ".join(f"{k} -> {[x for x in v if x.startswith('ETM')]}" for k, v in out.items()))
    # COUNT is clamped to 10-200 on purpose (constrain), so that half is current, intended behaviour.
    assert _has(out["?ETM,COUNT"], "ETM char message count set to 10") and _has(out["?ETM,COUNT,500"], "ETM char message count set to 200")
    zeroed = [cmd for cmd, line in (("?ETM,TIMEOUT", "ETM timeout set to 0 ms"), ("?ETM,BOOT", "ETM boot window set to 0 sec"),
                                    ("?ETM,DELAY", "ETM char delay set to 0 ms")) if _has(out[cmd], line)]
    negative = _has(out["?ETM,TIMEOUT,-5"], "ETM timeout set to -5 ms")
    assert not zeroed and not negative, f"persisted 0 for {zeroed}; negative timeout accepted: {negative}"


@test("etm.legacy_forms", "Legacy ?ETMxxx spellings set the same settings; mixed case is rejected", needs=["wcb1"], links=[])
def legacy_forms(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        checks = [("?ETMHB0", f"Invalid ETM heartbeat. Use 1-3600 seconds (currently {orig['HB']})."), ("?ETMHB12", "ETM heartbeat set to 12 sec"),
                  ("?ETMTIMEOUT650", "ETM timeout set to 650 ms"), ("?ETMBOOT3", "ETM boot window set to 3 sec"),
                  ("?ETMCHARCOUNT5", "ETM char count set to 10"), ("?ETMCHARDELAY150", "ETM char delay set to 150 ms"),
                  ("?etmmiss6", "ETM missed heartbeats set to 6"), ("?EtmMiss6", "Unknown command: EtmMiss6"),
                  ("?ETMOFF", "ETM disabled"), ("?ETMON", "ETM enabled")]     # OFF for one command: ON follows at once
        try:
            bad += [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
            cfg = _cfg(w)
            bad += [f"?config lacks {x!r}" for x in ("Heartbeat interval:   12 sec (+/- 1)", "Boot heartbeat:       1-3 sec",
                                                     "Offline after:        6 missed heartbeats (78 sec max)", "Retry timeout:        650 ms",
                                                     "Char message count:   10  delay: 150 ms") if not _has(cfg, x)]
        finally:
            w.run("?ETM,ON")          # never leave W1 with ETM off: WDP and every ACK depend on it (rule 4)
            _restore_etm(w, orig)
    assert not bad, "; ".join(bad)


@test("etm.legacy_miss0_rejected", "(should) Legacy ?ETMMISS0 is rejected like ?ETM,MISS,0 instead of marking every peer offline", needs=["wcb1"], links=[])
def legacy_miss0_rejected(bench):
    """Probable bug: legacy ?ETMMISS<n> writes etmMissedHeartbeats unchecked (WCB.ino:5912-5915); its new-form twin
    rejects 0 (5234-5236) and legacy ?ETMHB beside it has the guard (5902-5911). With MISS 0 every peer goes offline and
    ensured sends become fire-once (WCB.ino:1276). Restored within a second, in a finally."""
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        m = w.dev.mark()
        try:
            new_form = w.run("?ETM,MISS,0")
            legacy = w.run("?ETMMISS0")
            time.sleep(1.0)
            offline = _has(w.dev.since(m), "[ETM] WCB2 went OFFLINE (no heartbeat for 0s)")
        finally:
            w.run(f"?ETM,MISS,{orig['MISS']}")
            w.run("?WDP,POLL")
            try:
                w.dev.expect(r"\[ETM\] WCB2 came ONLINE", timeout=15, since=m)
            except AssertionError:
                pass
    bench.note(f"?ETMMISS0: {[x for x in legacy if 'ETM' in x]}; W2 went offline: {offline}")
    assert _has(new_form, "Invalid ETM missed-heartbeat count '0'. Use 1-100."), "the new form accepted 0"
    assert not _has(legacy, "ETM missed heartbeats set to 0"), "legacy ?ETMMISS0 bypassed the 1-100 check"


# ============================================================ ?STATS
@test("stats.rpt_local", "?STATS,RPT stores a 'Reported by Other Nodes' row silently; malformed rows are rejected; RESET clears it", needs=["wcb1"], links=[])
def rpt_local(bench):
    w = usb_wcb(bench)
    need8 = "[STATS] RPT ignored — need 8 fields (from,sent,ackd,retries,failed,unguaranteed,bcast,recv), got {}"
    bad = []
    if [x for x in w.run("?STATS") if x.startswith("WCB7: ")]:
        raise Skip("WCB7 already reports to W1")
    out = [x for x in w.run("?STATS,RPT,7,101,102,103,104,105,106,107") if x.startswith("[STATS]")]
    if out:
        bad.append(f"a valid RPT printed {out}")
    stats = [x.rstrip() for x in w.run("?STATS")]
    if "------------- Reported by Other Nodes -------------" not in stats or not any(
            re.match(r"^WCB7: Sent: 101, ACKd: 102, Retries: 103, Failed: 104, Unguaranteed: 105, Bcast: 106, Recv: 107  \([01]s ago\)$", x) for x in stats):
        bad.append("?STATS lacks the reported WCB7 row")
    for cmd, want in (("?STATS,RPT,7,1,2", need8.format(3)), ("?STATS,RPT", need8.format(0)),
                      ("?STATS,RPT,21,1,2,3,4,5,6,7", "[STATS] RPT ignored — reporter id 21 out of range (1-20)"),
                      ("?STATS,RPT,0,1,2,3,4,5,6,7", "[STATS] RPT ignored — reporter id 0 out of range (1-20)"),
                      ("?STATS,RPT,7,1,,3,4,5,6,7", need8.format(2)), ("?STATS,RESET", "ESP-NOW statistics reset.")):
        if not _has(w.run(cmd), want):
            bad.append(f"{cmd} lacks {want!r}")
    if [x for x in w.run("?STATS") if x.startswith("WCB7: ")]:
        bad.append("RESET left the WCB7 row")
    if not _has(w.run("?STATSRESET"), "ESP-NOW statistics reset."):
        bad.append("legacy ?STATSRESET")
    assert not bad, "; ".join(bad)


@test("stats.rpt_remote", "RPT and RESET delivered over the mesh are visible through ?MGMT,STATS,2", needs=["wcb1"], links=[])
def rpt_remote(bench):
    w = usb_wcb(bench)

    def pull():
        for _ in range(2):           # one STATS_REQ, no repeat: a lost request needs a retry (WCB.ino:3882-3892)
            m = w.dev.mark()
            w.send("?MGMT,STATS,2")
            try:
                w.dev.expect(r"^\[MGMT:STATS,2\]$", timeout=5, since=m)
                w.dev.expect(r"^--- End of ESP-NOW Statistics ---", timeout=5, since=m)
                return [x.rstrip() for x in w.dev.since(m)]
            except AssertionError:
                continue
        raise AssertionError("?MGMT,STATS,2 got no reply twice")

    w.send(";W2,?STATS,RPT,9,11,22,33,44,55,66,77")
    time.sleep(0.5)
    first = pull()
    w.send(";W2,?STATS,RESET")
    time.sleep(0.5)
    second = pull()
    assert _has(first, "--- WCB2 ESP-NOW Statistics (Since Last Reboot) ---"), first[:3]
    assert any(re.match(r"^WCB9: Sent: 11, ACKd: 22, Retries: 33, Failed: 44, Unguaranteed: 55, Bcast: 66, Recv: 77  \(\d+s ago\)$", x)
               for x in first), "no WCB9 row in the first pull"
    assert not [x for x in second if x.startswith("WCB9: ")], "RESET over the mesh left the WCB9 row"


# ============================================================ misc
@test("misc.identify", "?IDENTIFY prints its line and refuses to re-enter while running", needs=["wcb1"], links=[])
def identify(bench):
    w = usb_wcb(bench)
    first, second = w.run("?IDENTIFY"), w.run("?IDENTIFY")
    time.sleep(6)
    third = w.run("?IDENTIFY")
    line = "Identifying board for 5 seconds (red/green blink)..."
    assert _has(first, line) and _has(second, "Identify already running") and _has(third, line), (first, second, third)


@test("misc.led_query_invalid", "?LED query and out-of-range pins (no NVS write)", needs=["wcb1"], links=[])
def led_query_invalid(bench):
    """Never send a pin that toInt()s into 0-48, '?LED,PIN,abc' included: it writes NVS led_config with no restore on
    HW 1.0/2.4."""
    w = usb_wcb(bench)
    q = [x.rstrip() for x in w.run("?LED")]
    assert any(re.match(r"^LED pin: GPIO\d+$", x) for x in q) and "Use: ?LED,PIN,<gpio_number>" in q, q
    assert _has(w.run("?LED,PIN,49"), "Invalid LED pin 49. Valid GPIO range: 0-48")
    assert _has(w.run("?LED,PIN,-1"), "Invalid LED pin -1. Valid GPIO range: 0-48")


# ============================================================ ETM delivery (W1 deafened by a live ?MAC,3 change)
def _timed(dev, since):
    """[(host monotonic time, line)] after the mark."""
    return list(dev.lines[since:])


def _first(entries, pattern):
    rx = re.compile(pattern)
    return next((t for t, x in entries if rx.search(x)), None)


def _require_w2_online(w, wait=30):
    """W2 is often still rebooting from the test before (or from a config push); it is back online at its boot
    announce or next ETM heartbeat, so wait for that before giving up."""
    deadline = time.monotonic() + wait
    while not any(x.startswith("WCB2: ") and "Online" in x for x in w.run("?STATS")):
        if time.monotonic() > deadline:
            raise Skip(f"W1's ?STATS does not show WCB2 online after {wait} s")
        time.sleep(2)


@contextmanager
def _deaf_w1(w, tokens):
    """'?MAC,3,<other>' changes W1's receive filter at once (WCB.ino:4034) while its radio address only changes at
    boot (WCB.ino:8034-8051), so W1 still transmits but hears nothing: every ACK is lost. The octet is written to NVS
    at once (WCB_Storage.cpp:393-398), so it is restored first thing, and W1 is never reset in the window."""
    t = token(tokens, "?MAC,3,")
    if t is None:
        raise Skip("config chain lacks ?MAC,3")
    orig = t[len("?MAC,3,"):]
    other = "%02X" % (int(orig, 16) ^ 0x01)
    try:
        out = w.run(f"?MAC,3,{other}")
        if not _has(out, f"Updated 3rd MAC octet to 0x{other}"):
            raise AssertionError(f"?MAC,3,{other} printed {out}")
        yield
    finally:
        w.run(f"?MAC,3,{orig}")


@test("etm.ack_lost_retry_fail", "With W1 deafened a unicast retries 3 times at the ?ETM,TIMEOUT spacing and fails, while W2 executes it exactly once", needs=["wcb1"])
def ack_lost_retry_fail(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        _require_w2_online(w)
        try:
            w.run("?DEBUG,ETM,ON")
            for label, spacing in (("A", int(orig["TIMEOUT"])), ("B", 300)):
                if label == "B":
                    w.run("?ETM,TIMEOUT,300")
                w.run("?STATS,RESET")
                t = marker(label)
                pm, wm = s2.mark(), w.dev.mark()
                with _deaf_w1(w, before[1]):
                    w.send(f";W2,;S2{t}")
                    time.sleep(3.0)
                entries = _timed(w.dev, wm)
                sent = _first(entries, rf"\[ETM\] Sent seq \d+: ;S2{t}$")
                retries = [x for x, line in entries if re.search(rf"\[ETM\] Retry [123] to WCB2 for seq \d+: ;S2{t}$", line)]
                failed = _first(entries, rf"\[ETM\] WCB2 failed to ACK seq \d+ after 3 retries: ;S2{t}$")
                stats = [x.rstrip() for x in w.run("?STATS")]
                runs = s2.received(pm).count(t.encode() + b"\r")
                if sent is None or len(retries) != 3 or failed is None:
                    bad.append(f"{label}: sent {sent is not None}, {len(retries)} retries, failed {failed is not None}")
                else:
                    offsets = [round((r - sent) * 1000) for r in retries]
                    if any(not k * spacing - 50 <= o <= k * spacing + 250 for k, o in enumerate(offsets, 1)):
                        bad.append(f"{label}: retry offsets {offsets} ms at TIMEOUT {spacing}")
                    fail_ms = round((failed - sent) * 1000)
                    if not 4 * spacing - 50 <= fail_ms <= 4 * spacing + 600:
                        bad.append(f"{label}: failure line {fail_ms} ms after Sent")
                if _has([line for _, line in entries], "ACK received from WCB2"):
                    bad.append(f"{label}: an ACK got through to a deafened W1")
                if runs != 1:
                    bad.append(f"{label}: W2 ran the command {runs} times")
                if not any(re.match(r"^WCB2: Sent: 1, ACKd: 0, Retries: 3, Failed: 1, Online", x) for x in stats):
                    bad.append(f"{label}: stats {[x for x in stats if x.startswith('WCB2: ')]}")
        finally:
            w.run(f"?ETM,TIMEOUT,{orig['TIMEOUT']}")
            w.run("?DEBUG,ETM,OFF")
    assert not bad, "; ".join(bad)


@test("etm.dedup_ring_covers_pending", "(should) Nine in-flight unicasts to one board whose ACKs are lost each execute once, as eight do", needs=["wcb1"])
def dedup_ring_covers_pending(bench):
    """Probable bug: the receiver's per-sender duplicate ring ETM_SEQ_HISTORY=8 (WCB.ino:591) is smaller than the
    sender's pending table ETM_PENDING_MAX=10 (WCB.ino:541), so a burst of 9-10 ensured commands whose ACKs are lost
    re-executes on every retry round. Eight is the control."""
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    counts = {}
    with config_guard(bench, 1) as before:
        _require_w2_online(w)
        for n in (8, 9):
            ms = [marker(f"{n}{k}") for k in range(n)]
            pm = s2.mark()
            with _deaf_w1(w, before[1]):
                w.send("^".join(f";W2,;S2{x}" for x in ms))     # the sender splits it into n unicasts
                time.sleep(4.0)
            time.sleep(1.0)
            got = s2.received(pm)
            counts[n] = [got.count(x.encode() + b"\r") for x in ms]
    bench.note(f"executions per command with lost ACKs: {counts}")
    assert counts[8] == [1] * 8, f"control (8 in flight): {counts[8]}"
    assert counts[9] == [1] * 9, f"9 in flight re-executed: {counts[9]}"


@test("etm.pending_table_evict", "The 11th in-flight unicast evicts the oldest pending entry and counts it failed", needs=["wcb1"])
def pending_table_evict(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    ms = [marker(f"e{k}") for k in range(11)]
    with config_guard(bench, 1) as before:
        _require_w2_online(w)
        try:
            w.run("?DEBUG,ETM,ON")
            w.run("?STATS,RESET")
            pm, wm = s2.mark(), w.dev.mark()
            with _deaf_w1(w, before[1]):
                w.send("^".join(f";W2,;S2{x}" for x in ms))
                time.sleep(4.0)
            lines = [x.rstrip() for x in w.dev.since(wm)]
            stats = [x.rstrip() for x in w.run("?STATS")]
            got = s2.received(pm)
        finally:
            w.run("?DEBUG,ETM,OFF")
    failed = [k for k, x in enumerate(ms) if any("failed to ACK seq" in line and line.endswith(f";S2{x}") for line in lines)]
    runs = [got.count(x.encode() + b"\r") for x in ms]
    bench.note(f"executions per command: {runs}")
    assert sum("Pending table full, evicting oldest entry" in x for x in lines) == 1, "not exactly one eviction line"
    assert failed == list(range(1, 11)), f"failure lines for commands {failed}, expected 1-10 (the evicted 0th has none)"
    assert sum("[ETM] Retry" in x for x in lines) == 30, f"{sum('[ETM] Retry' in x for x in lines)} retry lines"
    assert _has(stats, "Transmission Attempts: 11, Delivered: 0, Failed: 11"), "summary counts"
    assert any(re.match(r"^WCB2: Sent: 11, ACKd: 0, Retries: 30, Failed: 11", x) for x in stats), "WCB2 row"
    assert got.count(ms[0].encode() + b"\r") == 1 and all(x.encode() + b"\r" in got for x in ms[1:]), "delivery on W2 S2"


@test("etm.acked_not_executed_reboot", "(should) A command W2 ACKs while its ?reboot is pending is not silently lost (W2 reboot)", needs=["wcb1"])
def acked_not_executed_reboot(bench):
    """The rule-11 pattern: reboot() is delay(2000) + ESP.restart() inline (WCB.ino:825-829), and W2's WiFi task ACKs
    while loop() sits in that delay (WCB.ino:4254-4272), so a command already ETM-ACKed and queued behind it is lost.
    ?ERASE,NVS and ?PX do the same. A command sent after the boot is the positive control."""
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    t, ctl = marker("lost"), marker("ctl")
    try:
        w.run("?DEBUG,ETM,ON")
        wm = w.dev.mark()
        w.send(";W2,?reboot")
        w.dev.expect(r"\[ETM\] Seq \d+ fully acknowledged", timeout=3, since=wm)
        time.sleep(0.3)
        pm, am = s2.mark(), w.dev.mark()
        w.send(f";W2,;S2{t}")
        seq = w.dev.expect(rf"\[ETM\] Sent seq (\d+): ;S2{t}", timeout=3, since=am).group(1)
        try:
            w.dev.expect(rf"\[ETM\] Seq {seq} fully acknowledged", timeout=3, since=am)
            acked = True
        except AssertionError:
            acked = False
        w.dev.expect(r"\[ETM\] WCB2 came ONLINE \(boot\)", timeout=25, since=wm)
        time.sleep(3)
        delivered = t.encode() in s2.received(pm)
        m = s2.mark()
        w.send(f";W2,;S2{ctl}")
        s2.expect(ctl.encode() + b"\r", timeout=3, since=m)
    finally:
        w.run("?DEBUG,ETM,OFF")
    bench.note(f"command during W2's reboot delay: ACKed {acked}, executed {delivered}")
    assert delivered or not acked, "W2 ACKed the command during its reboot delay and never ran it"


@test("etm.offline_detection_timing", "The offline threshold (HB+1)*MISS uses W1's own settings: HB 4 / MISS 1 marks W2 offline ~5 s after its last packet (slow, ~45 s)", needs=["wcb1"], links=[])
def offline_detection_timing(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        try:
            w.run("?DEBUG,ETM,ON")
            w.run("?ETM,HB,4")
            w.run("?ETM,MISS,1")
            cfg_ok = _has(_cfg(w), "Offline after:        1 missed heartbeats (5 sec max)")
            wm = w.dev.mark()
            time.sleep(40)
            entries = _timed(w.dev, wm)
        finally:
            _restore_etm(w, orig, ("HB", "MISS"))
            w.run("?DEBUG,ETM,OFF")
            w.run("?WDP,POLL")
            time.sleep(2)
    gaps, last_seen, state, unpaired = [], None, None, []
    for t, line in entries:
        if re.search(r"\[ETM\] (Heartbeat from WCB2|WCB2 came ONLINE)(?!\d)", line):   # not NaviCore's WCB20
            last_seen = t
        elif "[ETM] WCB2 went OFFLINE (no heartbeat for 5s)" in line and last_seen is not None:
            gaps.append(round(t - last_seen, 2))
        # Tracker #80's fingerprint, whatever the gaps: W2's edges alternate. The race printed a false OFFLINE as W2's
        # packet arrived (two OFFLINEs with no ONLINE between) or lost one (an ONLINE with no OFFLINE before it). A
        # boot announce re-prints ONLINE whatever the state (espNowReceiveCallback, WCB.ino), so it only sets it.
        if "[ETM] WCB2 went OFFLINE" in line:
            if state == "off":
                unpaired.append(f"OFFLINE at {t:.2f}")
            state = "off"
        elif "[ETM] WCB2 came ONLINE" in line:
            if state == "on" and "(boot)" not in line:
                unpaired.append(f"ONLINE at {t:.2f}")
            state = "on"
    nexts = [int(m.group(1)) for _, line in entries for m in [re.search(r"\[ETM\] Next heartbeat in (\d+)ms", line)] if m]
    bench.note(f"offline gaps {gaps} s; next-heartbeat delays {nexts}")
    assert cfg_ok, "?config does not show '1 missed heartbeats (5 sec max)'"
    assert gaps, "W2 never went offline with HB 4 / MISS 1"
    assert not unpaired, f"W2's edges do not alternate (tracker #80's race): {unpaired}, each with none of the other kind since"
    # The floor is the firmware's (it clears a board only once its age is past the threshold, boardSweepOffline in
    # WCB.ino) less the host's timing error. A line is stamped when SerialDevice splits it out of a read
    # (hil/serialdev.py): alone in its read it is good to one Windows timer tick (~16 ms), but with more output behind it
    # in the same read it is stamped when that read ends, up to one read (60-95 ms) late. A Heartbeat line that shared
    # its read with NaviCore's rc_hb gave 4.93 s (run 20260924-234056); every gap without that has been 4.985 s or more.
    # #80 made gaps of about 10 s, which the alternation check above catches even when only one gap is hit.
    assert min(gaps) >= 4.85 and sorted(gaps)[len(gaps) // 2] <= 6.5, f"offline gaps {gaps}"
    assert all(3000 <= n < 5000 for n in nexts[1:]), f"heartbeat delays {nexts}"


@test("etm.offline_cancels_retry", "A peer going offline mid-retry prints the cancel line instead of the 3-retry failure", needs=["wcb1"])
def offline_cancels_retry(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    m0, m1 = marker("p"), marker("c")
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        mac = token(before[1], "?MAC,3,")
        if mac is None:
            raise Skip("config chain lacks ?MAC,3")
        orig_oct = mac[len("?MAC,3,"):]
        other = "%02X" % (int(orig_oct, 16) ^ 0x01)
        _require_w2_online(w)
        try:
            w.run("?ETM,TIMEOUT,1500")
            w.run("?DEBUG,ETM,ON")
            w.run("?STATS,RESET")
            pm, wm = s2.mark(), w.dev.mark()
            w.send(f";W2,;S2{m0}")
            w.dev.expect(r"\[ETM\] Seq \d+ fully acknowledged", timeout=3, since=wm)
            cm = w.dev.mark()
            # One line, so HB/MISS change, W1 goes deaf and the send happen in one drain (see the spec's timing note).
            w.send(f"?ETM,HB,1^?ETM,MISS,1^?MAC,3,{other}^;W2,;S2{m1}")
            time.sleep(4.5)
        finally:
            w.run(f"?MAC,3,{orig_oct}")          # first: the octet is already in NVS
            _restore_etm(w, orig, ("MISS", "HB", "TIMEOUT"))
            w.run("?DEBUG,ETM,OFF")
        lines = [x.rstrip() for x in w.dev.since(cm)]
        stats = [x.rstrip() for x in w.run("?STATS")]
        got = s2.received(pm)
    seq = next((m.group(1) for x in lines for m in [re.search(rf"\[ETM\] Sent seq (\d+): ;S2{m1}$", x)] if m), None)
    assert seq, "no Sent line for the command"
    assert _has(lines, f"[ETM] WCB2 offline, canceling retry for seq {seq}"), "no cancel line"
    assert not _has(lines, f"failed to ACK seq {seq}") and not _has(lines, f"Retry 2 to WCB2 for seq {seq}"), "it retried on and failed instead"
    assert got.count(m0.encode() + b"\r") == 1 and got.count(m1.encode() + b"\r") == 1, f"W2 S2 got {got!r}"
    assert any(re.match(r"^WCB2: Sent: 2, ACKd: 1, Retries: 1, Failed: 1, ", x) for x in stats), f"stats {[x for x in stats if x.startswith('WCB2: ')]}"


@test("etm.chksm_mismatch_silent_loss", "Characterization: with a ?ETM,CHKSM mismatch the target ACKs and then discards, so ?STATS counts it delivered", needs=["wcb1"])
def chksm_mismatch_silent_loss(bench):
    """Design trap: the ACK precedes the CRC check (WCB.ino:4269-4272 vs 4304-4311). Rule 4 says a CHKSM mismatch
    rejects all packets; the sender cannot see that, and ?STATS shows 100 % delivery while nothing runs. Only W1 is
    switched, for under 5 s: while off, W1 runs inbound commands with the '|CRC' suffix still attached."""
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    t, t2 = marker("x"), marker("y")
    with config_guard(bench, 1):
        _require_w2_online(w)
        try:
            w.run("?DEBUG,ETM,ON")
            w.run("?STATS,RESET")
            off = w.run("?ETM,CHKSM,OFF")
            pm, wm = s2.mark(), w.dev.mark()
            w.send(f";W2,;S2{t}")
            time.sleep(3)
            lines, lost = w.dev.since(wm), t.encode() not in s2.received(pm)
            stats = [x.rstrip() for x in w.run("?STATS")]
        finally:
            on = w.run("?ETM,CHKSM,ON")
        m = s2.mark()
        w.send(f";W2,;S2{t2}")
        try:
            s2.expect(t2.encode() + b"\r", timeout=3, since=m)
            control = True
        except AssertionError:
            control = False
        w.run("?DEBUG,ETM,OFF")
    assert _has(off, "ETM checksum verification disabled") and _has(on, "ETM checksum verification enabled")
    assert _has(lines, "fully acknowledged"), "W2 did not ACK the CRC-less command"
    assert lost, "W2 executed a command with no CRC"
    assert any(re.match(r"^WCB2: Sent: 1, ACKd: 1, Retries: 0, Failed: 0", x) for x in stats), "?STATS did not count it delivered"
    assert control, "after CHKSM ON the command was not delivered"


@test("etm.chksm_length_limit", "Under ?ETM,CHKSM a 187-character command is sent and runs; 188 is refused before sending", needs=["wcb1"])
def chksm_length_limit(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?ETM,CHKSM,ON")
    fits, over = padded("L", 184), padded("M", 185)            # ';S2' + 184 = 187 (ETM_MAX_CMD_WITH_CRC, WCB.ino:225)
    pm = s2.mark()
    w.send(f";W2,;S2{fits}")
    s2.expect(fits.encode() + b"\r", timeout=3, since=pm)
    pm, wm = s2.mark(), w.dev.mark()
    w.send(f";W2,;S2{over}")
    time.sleep(3)
    assert _has(w.dev.since(wm), "[ETM] Command 188 chars exceeds the 187-char limit under ?ETM,CHKSM"), "no refusal line"
    assert over.encode() not in s2.received(pm), "the 188-character command was delivered"


@test("etm.off_local", "W1 ?ETM,OFF (under 40 s): W2 drops W1's text but runs its ;M whitelist; POLL and CHAR refuse; ?config/?STATS formats", needs=["wcb1"])
def off_local(bench):
    """While off, W1 sends no heartbeats (WCB.ino:1092) or WDP adverts (WCB_WDP.cpp:356). ;M2,getErrors clears the real
    Maestro 2's error flags, which the bench's port_stimulus already does."""
    s2, tap = link(bench, 2, "S2"), link(bench, 2, "S1")
    w = usb_wcb(bench)
    t, t3 = marker("a"), marker("c")
    bad = []
    with config_guard(bench, 1) as before:
        if "?ETM,ON" not in before[1]:
            raise Skip("ETM is already off on W1")
        try:
            w.run("?DEBUG,ETM,ON")
            if not _has(w.run("?ETM,OFF"), "ETM disabled"):
                bad.append("?ETM,OFF did not confirm")
            pm, tm, wm = s2.mark(), tap.mark(), w.dev.mark()
            w.send(f";W2,;S2{t}")
            w.send(";W2,;M2,getErrors")
            poll, char, cfg = w.run("?WDP,POLL"), w.run("?ETM,CHAR"), _cfg(w)
            stats = w.run("?STATS")
            time.sleep(10)
            lines, got2, tapped = w.dev.since(wm), s2.received(pm), tap.received(tm)
        finally:
            on = w.run("?ETM,ON")
            w.run("?DEBUG,ETM,OFF")
        m = s2.mark()
        w.send(f";W2,;S2{t3}")
        try:
            s2.expect(t3.encode() + b"\r", timeout=3, since=m)
        except AssertionError:
            bad.append("delivery did not resume after ?ETM,ON")
    if t.encode() in got2:
        bad.append("W2 ran non-ETM text while its ETM is on")
    if bytes.fromhex("AA0221") not in tapped:
        bad.append("the ;M whitelist did not reach W2's Maestro")
    if not _has(poll, "[WDP] POLL needs WDP + ETM enabled"):
        bad.append("POLL did not refuse")
    if not _has(char, "ETM is not enabled. Use ?ETMON first."):
        bad.append("CHAR did not refuse")
    if not _has(cfg, "ETM:                  Disabled") or _has(cfg, "Heartbeat interval"):
        bad.append("?config ETM block")
    if _has(stats, "--------------- ETM Per-Board Statistics ---------------"):
        bad.append("?STATS still has the per-board block")
    if not _has(lines, "[ETM] Received ETM packet but ETM disabled locally, ignoring."):
        bad.append("no 'ETM disabled locally' line in 10 s")
    if not _has(on, "ETM enabled"):
        bad.append("?ETM,ON did not confirm")
    assert not bad, "; ".join(bad)


@test("etm.broadcast_per_board_ack", "A plain broadcast is tracked once per online peer: W2 ACKs it once; the controller's ACK may or may not be counted", needs=["wcb1"])
def broadcast_per_board_ack(bench):
    s12, s23 = link(bench, 1, "S2"), link(bench, 2, "S3")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,OUT,S0,OFF")
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON")
    w = usb_wcb(bench)
    t = f"hilbc{nonce().lower()}"            # reaches every broadcast port on both WCBs: inert text only
    try:
        w.run("?DEBUG,ETM,ON")
        w.run("?STATS,RESET")
        watch, wm = Watch(s12, s23), w.dev.mark()
        w.send(t)
        watch.expect(s12, t.encode() + b"\r")
        watch.expect(s23, t.encode() + b"\r")
        time.sleep(1.5)
        lines = [x.rstrip() for x in w.dev.since(wm)]
        stats = [x.rstrip() for x in w.run("?STATS")]
    finally:
        w.run("?DEBUG,ETM,OFF")
    seq = next((m.group(1) for x in lines for m in [re.search(rf"\[ETM\] Sent seq (\d+): {t}$", x)] if m), None)
    assert seq, "no Sent line for the broadcast"
    assert f"[ETM] ACK received from WCB2 for seq {seq}" in lines and f"[ETM] Seq {seq} fully acknowledged" in lines, "W2 ACK lines"
    controller = f"[ETM] ACK received from WCB20 for seq {seq}" in lines
    bench.note(f"NaviCore's broadcast ACK counted: {controller}")
    assert any(re.match(r"^WCB2: Sent: 1, ACKd: 1, Retries: 0, Failed: 0", x) for x in stats), "WCB2 row"


@test("stats.delivered_not_above_attempts", "(should) ?STATS never reports more deliveries than transmissions: an ACK nobody expected is not counted", needs=["wcb1"], links=[])
def delivered_not_above_attempts(bench):
    """Probable stats bug: etmProcessAck (WCB.ino:1301-1303) counts any ACK matching an active entry without checking
    expectAckFrom. NaviCore (WCB_Client) ACKs every broadcast (WCB_Client.cpp:2748) although it is not expected
    (WCB.ino:1265-1272), so Delivered can exceed Transmission Attempts, depending on which ACK lands first."""
    w = usb_wcb(bench)
    if not any("WCB20" in x for x in w.run("?STATS")):
        raise Skip("the controller (NaviCore 20) is not in W1's stats")
    w.run("?STATS,RESET")
    for _ in range(6):
        w.send(f"hilbc{nonce().lower()}")
        time.sleep(0.8)
    time.sleep(1.0)
    stats = "\n".join(w.run("?STATS"))
    m = re.search(r"Transmission Attempts: (\d+), Delivered: (\d+), Failed: (\d+)", stats)
    assert m, "no Transmission Attempts line"
    bench.note(f"after 6 broadcasts: {m.group(0)}")
    assert int(m.group(2)) <= int(m.group(1)), f"Delivered {m.group(2)} > Attempts {m.group(1)}"


# ============================================================ peers, controller, alias
def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _live_peers(w):
    line = next((x.rstrip() for x in w.run("?PEERSLIVE") if x.startswith("Live peers: ")), "")
    m = re.match(r"^Live peers: (\d+) \(WCBQ floor (\d+)(?: \+ learned, auto-join ON)?\)$", line)
    if not m:
        raise AssertionError(f"?PEERSLIVE printed {line!r}")
    return int(m.group(1)), line


def _dump(w):
    return [x.rstrip() for x in w.run("?WDP,DUMP", timeout=8)]


def _row(dump, n):
    return next((x for x in dump if x.startswith(f"[WDP:N={n},")), None)


def _field(row, name):
    m = re.search(rf"[,\[]{name}=([^,\]]*)", row or "")
    return m.group(1) if m else None


@test("peers.peerslive_consistency", "?PEERSLIVE, the WDPCFG and ?WDP,STATUS peer counts and the ?config peer list agree", needs=["wcb1"], links=[])
def peerslive_consistency(bench):
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    n, line = _live_peers(w)
    assert line in [x.rstrip() for x in w.run("?PEERSLIVE,99")], "?PEERSLIVE,99 differs (the argument should be ignored)"
    dump = _dump(w)
    cfgline = next((x for x in dump if x.startswith("[WDPCFG:")), "")
    k = sum(1 for x in dump if x.startswith("[WDP:N=") and not x.startswith(f"[WDP:N={me},"))
    status = next((x.rstrip() for x in w.run("?WDP,STATUS") if x.startswith("[WDP:en=")), "")
    peers = [x for x in _cfg(w) if re.match(r"^  WCB\d+: ", x) and "(this board)" not in x and "(controller)" not in x]
    bench.note(f"live peers {n}; WDP neighbours {k}; ?config peer lines {len(peers)}")
    assert cfgline.endswith(f"PEERS={n}]"), cfgline
    assert _has(dump, f"[WDP:END,count={k}]"), "DUMP END count differs from its rows"
    assert status.endswith(f"neighbors={k},peers={n}]"), status
    assert len(peers) == n, f"?config lists {len(peers)} peers"
    with Console(bench, 2) as c2:
        assert _has(_crun(c2, "?PEERSLIVE"), "Live peers: "), "W2 did not answer ?PEERSLIVE"


@test("peers.wcbq_live", "?WCBQ,3 registers WCB3 live (never seen: fire-once, no retry); invalid values are rejected; restoring 2 removes it", needs=["wcb1"], links=[])
def wcbq_live(bench):
    """Never below the real fleet (2): W2 would auto-join as a PERSISTED learned peer on its next advert."""
    w = usb_wcb(bench)
    bad = []
    t = marker()
    with config_guard(bench, 1) as before:
        if token(before[1], "?WCBQ,") != "?WCBQ,2":
            raise Skip("W1's WCBQ is not 2")
        n, _ = _live_peers(w)
        try:
            w.run("?DEBUG,ETM,ON")
            for cmd, want in (("?WCBQ", "Invalid WCB quantity 0. Valid range: 1-20."), ("?WCBQ,21", "Invalid WCB quantity 21. Valid range: 1-20."),
                              ("?WCBQ,3", "Saved WCB quantity: 3. Peer registrations reconciled live (no reboot needed).")):
                if not _has(w.run(cmd), want):
                    bad.append(f"{cmd} lacks {want!r}")
            if not _live_peers(w)[1].startswith(f"Live peers: {n + 1} (WCBQ floor 3"):
                bad.append(f"after ?WCBQ,3: {_live_peers(w)[1]}")
            cfg = _cfg(w)
            if not _has(cfg, "Number of WCBs in the system: 3") or not any(re.match(r"^  WCB3: 02:[0-9A-F]{2}:[0-9A-F]{2}:00:00:03  Not yet seen", x) for x in cfg):
                bad.append("?config does not list WCB3 as not yet seen")
            wm = w.dev.mark()
            w.send(f";W3,;S0{t}")
            time.sleep(1.2)
            lines = w.dev.since(wm)
            if not _has(lines, f";S0{t}") or not any(re.search(r"\[ETM\] Seq \d+ resolved", x) for x in lines) \
                    or _has(lines, "Retry") or _has(lines, "failed to ACK"):
                bad.append("the send to a never-seen WCB3 was not fire-once")
            if not any(re.match(r"^WCB3: Sent: 0, ACKd: 0, Retries: 0, Failed: 0, OFFLINE", x) for x in w.run("?STATS")):
                bad.append("WCB3 stats row")
        finally:
            w.run("?WCBQ,2")
            w.run("?DEBUG,ETM,OFF")
        if _live_peers(w)[0] != n:
            bad.append("restoring WCBQ 2 did not drop WCB3")
        if not _has(w.run(";W3,x"), "WCB 3 is not a reachable target — it isn't a configured or learned peer or the controller."):
            bad.append(";W3 still reachable")
    assert not bad, "; ".join(bad)


@test("peers.controller_off_on", "?CONTROLLER status and validation; OFF makes WCB20 unreachable; ON,20 re-registers live (auto-join held off)", needs=["wcb1"], links=[])
def controller_off_on(bench):
    """Auto-join is turned off first: NaviCore advertises non-temporary, so with auto-join on and the controller off it
    would join as a persisted learned peer. The OFF window stays under 30 s.

    ON,20 prints "registered (live)" only when WCB20 is not already an ESP-NOW peer (enableControllerPeer, WCB.ino).
    OFF deletes that peer, but a disabled controller is not ignored on receive: an ETM command NaviCore sends during
    the OFF window (its 30 s ?STATS,RPT) is still ACKed, and etmSendAck re-adds the sender on demand. Then the line is
    rightly missing (run 20260923-154611). ETM debug is on so that ACK is visible: the missing line is excused only
    when an "[ETM] Sent ACK seq N to WCB20" falls between OFF's confirmation and ON - otherwise it still fails, since
    that line is the test's only check that OFF freed the slot."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    bad = []
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1] or _row(_dump(w), 20) is None:
            raise Skip("controller 20 is not enabled, or NaviCore is not in W1's WDP table")
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            w.run("?DEBUG,ETM,ON")          # shows an ACK to WCB20 inside the OFF window (see the docstring)
            for cmd in ("?CONTROLLER", "?SPECIAL"):
                out = [x.rstrip() for x in w.run(cmd)]
                if "Controller peer (ID 20) is currently ENABLED." not in out or "Use ?CONTROLLER,ON[,<id>] (1-20) or ?CONTROLLER,OFF" not in out:
                    bad.append(f"{cmd} printed {out}")
            for bad_id in (21, 0):
                if not _has(w.run(f"?CONTROLLER,ON,{bad_id}"), f"Invalid controller peer ID {bad_id}. Valid range: 1-20."):
                    bad.append(f"ON,{bad_id} accepted")
            off_mark = w.dev.mark()
            if not _has(w.run("?CONTROLLER,OFF"), "Controller peer (ID 20) DISABLED."):
                bad.append("OFF did not confirm")
            out = [x.rstrip() for x in w.run(";W20,?version")]
            if "WCB 20 is not a reachable target — it isn't a configured or learned peer." not in out:
                bad.append(f";W20 while off: {out}")
            cfg, stats, dump = _cfg(w), w.run("?STATS"), _dump(w)
            if _has(cfg, "WCB20 (controller)") or _has(cfg, "Controller Peer:") or _has(stats, "WCB20 (special)"):
                bad.append("the controller still shows in ?config or ?STATS")
            self_row = _row(dump, me)
            if _field(self_row, "CTRL") != "0" or int(_field(self_row, "CAP") or "0", 16) & 0x0040 or _field(_row(dump, 20), "PEER") != "0":
                bad.append(f"DUMP while off: {self_row} / {_row(dump, 20)}")
            on_mark = w.dev.mark()
            out = w.run("?CONTROLLER,ON,20")
            if not _has(out, "Controller peer (ID 20) ENABLED."):
                bad.append(f"ON,20 printed {out}")
            elif not _has(out, "Controller peer WCB20 registered (live)."):
                window = w.dev.since(off_mark)[:on_mark - off_mark]
                start = next((i for i, x in enumerate(window) if "Controller peer (ID 20) DISABLED." in x), None)
                acks = [x for x in (window[start:] if start is not None else [])
                        if re.search(r"\[ETM\] Sent ACK seq \d+ to WCB20\b", x)]
                if acks:
                    bench.note(f"ON,20: WCB20 was already an ESP-NOW peer - re-added by an ETM ACK during the OFF "
                               f"window ({acks[0].strip()})")
                else:
                    bad.append(f"ON,20 printed {out} and no ACK to WCB20 during the OFF window re-added the peer - "
                               f"OFF may not have freed it")
            wm = w.dev.mark()
            w.send(";W20,?version")
            try:
                w.dev.expect(r"^\[TERM:20\]End of Version", timeout=5, since=wm)
            except AssertionError:
                bad.append("NaviCore did not answer after ON,20")
        finally:
            if not _has(w.run("?CONTROLLER"), "is currently ENABLED"):
                w.run("?CONTROLLER,ON,20")
            w.run("?DEBUG,ETM,OFF")
            w.run("?WDP,AUTOJOIN,ON")
    assert not bad, "; ".join(bad)


@test("wdp.controller_auto_enable", "Hearing NaviCore as a NEW neighbour while the controller is off auto-enables it (auto-join does not gate this)", needs=["wcb1"], links=[])
def controller_auto_enable(bench):
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1] or _row(_dump(w), 20) is None:
            raise Skip("controller 20 is not enabled, or NaviCore is not in W1's WDP table")
        try:
            wm = w.dev.mark()
            w.send("?WDP,AUTOJOIN,OFF^?CONTROLLER,OFF^?WDP,FORGET,20^?WDP,POLL")    # one drain: no advert lands between
            first = last = time.monotonic()
            # The solicit and NaviCore's one solicited reply are each a single unacknowledged broadcast
            # (WCB_WDP.cpp:339-358, WCB_Client.cpp:2062-2077). NaviCore's next unsolicited advert is up to
            # 60 s away, so losing either frame leaves a 3 s window empty (full runs 09-21 and 09-22).
            # Re-polling changes nothing under test: WCB20 stays forgotten and the controller stays OFF
            # until an advert is actually decoded.
            lost = False
            for attempt in range(3):
                try:
                    w.dev.expect(r"Controller peer WCB20 registered \(live\)\.", timeout=3, since=wm)
                    if attempt:
                        bench.note(f"NaviCore was learned on poll {attempt + 1} of 3")
                    break
                except AssertionError:
                    if attempt < 2:
                        w.send("?WDP,POLL")
                        last = time.monotonic()
            else:
                lost = True
            time.sleep(0.3)
            lines = [x.rstrip() for x in w.dev.since(wm)]
            if lost:
                # Which frame was lost: W2 hears the same broadcasts. A NaviCore advert decoded by W2 inside
                # the polling window means NaviCore answered and W1 missed it; none means NaviCore never
                # answered (lost solicit, or the client no longer answers). A diagnostic only: never fails.
                age, window = "unread", None
                try:
                    with Console(bench, 2) as c2:
                        cm = c2.send("?WDP,DUMP")
                        window = time.monotonic() - first    # W2 stamps AGE as it prints; a relayed dump's END lands seconds later
                        try:
                            c2.expect(r"\[WDP:END,", timeout=8, since=cm)
                        except AssertionError:
                            pass
                        age = _field(_row([x.rstrip() for x in c2.lines(cm)], 20), "AGE") or "no N=20 row"
                except AssertionError as e:
                    age = f"unread ({str(e).splitlines()[0]})"
                bench.note(f"no NaviCore advert after 3 polls; W2's N=20 AGE={age}{' s' if age.isdigit() else ''}"
                           + (f", first POLL {window:.1f} s / last {window - (last - first):.1f} s before W2's DUMP"
                              " (AGE within that window: NaviCore answered and W1 missed it; older: NaviCore never answered)"
                              if window is not None else ""))
            query, dump = w.run("?CONTROLLER"), _dump(w)
        finally:
            if not _has(w.run("?CONTROLLER"), "is currently ENABLED"):
                w.run("?CONTROLLER,ON,20")
            w.run("?WDP,AUTOJOIN,ON")
    missing = [x for x in ("[WDP] auto-join disabled", "Controller peer (ID 20) DISABLED.", "[WDP] forgot WCB20",
                           "[WDP] polled: advertised + solicited the mesh", "[WDP] learned WCB20",
                           "(WCB20) — auto-enabling controller peer", "Controller peer (ID 20) ENABLED.",
                           "Controller peer WCB20 registered (live).") if not _has(lines, x)]
    assert not missing, f"missing {missing}"
    assert not _has(lines, "[WDP] auto-joined WCB20") and not _has(lines, "[PEER] WCB20 unregistered."), "NaviCore was joined as a peer"
    assert _has(query, "is currently ENABLED"), "?CONTROLLER not enabled afterwards"
    assert _field(_row(dump, 20), "PEER") == "0" and _field(_row(dump, me), "CTRL") == "20", "DUMP rows afterwards"


@test("alias.set_sanitize_propagate", "?ALIAS query, sanitising, truncation and clear; a new alias resolves from W2 through WDP", needs=["wcb1"])
def alias_set_sanitize_propagate(bench):
    s12 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    name = f"HIL{nonce()}"
    t, t2 = marker("a"), marker("b")
    bad = []
    with config_guard(bench, 1) as before:
        orig = token(before[1], "?ALIAS,")
        try:
            for cmd, want in (("?ALIAS,HIL,a;b?c", "WCB alias set to: HIL_a_b_c"), ("?ALIAS,ABCDEFGHIJKLMNOPQRSTUVWXYZ", "WCB alias set to: ABCDEFGHIJKLMNOPQRSTUVWX"),
                              (f"?ALIAS,{name}", f"WCB alias set to: {name}")):
                if not _has(w.run(cmd), want):
                    bad.append(f"{cmd} lacks {want!r}")
            time.sleep(2)             # the on-change advert is checked every 500 ms
            pm = s12.mark()
            w.send(f"?MGMT,FRAG,2,{nonce()[:4]},0,1,;W{name},;S2{t}")
            try:
                s12.expect(t.encode() + b"\r", timeout=3, since=pm)
            except AssertionError:
                bad.append("W2 did not resolve W1's new alias")
            wm = w.dev.mark()
            w.send(f";W{name},;S0{t2}")
            try:
                w.dev.expect(rf"^{t2}$", timeout=3, since=wm)
            except AssertionError:
                bad.append("W1's own alias did not route locally")
            if not _has(w.run("?ALIAS,CLEAR"), "WCB alias cleared") or not _has(w.run("?ALIAS"), "Alias: (not set)"):
                bad.append("CLEAR")
            if token(snapshot(bench, 1), "?ALIAS,"):
                bad.append("the chain still has an ?ALIAS token")
        finally:
            w.run(orig if orig else "?ALIAS,CLEAR")
            time.sleep(2)
    assert not bad, "; ".join(bad)


# ============================================================ command characters (dev.send while they are changed)
def _chains(lines):
    """(configured chain, factory chain) lines from ?backup / WCB_WEBTOOL_CONFIG_PULL output."""
    def after(marker_text):
        i = next((k for k, x in enumerate(lines) if marker_text in x), None)
        return next((x.strip() for x in lines[i + 1:] if re.search(r"CHK[0-9A-Fa-f]{8}\s*$", x)), "") if i is not None else ""
    return after("For Configured Boards"), after("For Factory Reset/Fresh Boards")


def _live_chars(w):
    """(delimiter, function identifier) from WCB_WEBTOOL_CONFIG_PULL, which is recognised whatever they are (WCB.ino:4877)."""
    m = w.dev.mark()
    w.dev.send("WCB_WEBTOOL_CONFIG_PULL")
    delim = w.dev.expect(r"For Configured Boards \(Current Delimiter: '(.)'\)", timeout=5, since=m).group(1)
    time.sleep(1.5)
    chain = _chains(w.dev.since(m))[0]
    return delim, (chain[:1] or "?")


def _restore_delim(w):
    delim, _ = _live_chars(w)
    if delim != "^":
        w.dev.send("?D^" if delim == "," else "?DELIM,^")     # the spelling must not contain the live delimiter
        time.sleep(0.5)


def _sent(w, line, pattern, timeout=3.0):
    m = w.dev.mark()
    w.dev.send(line)
    return w.dev.expect(pattern, timeout=timeout, since=m)


@test("chars.delim_change_restore", "?DELIM changes chain splitting and the backup's shape; the new and legacy setters restore it", needs=["wcb1"], links=[])
def delim_change_restore(bench):
    w = usb_wcb(bench)
    t = marker()
    with config_guard(bench, 1):
        try:
            _sent(w, "?DELIM,|", r"^Delimiter updated to: '\|'")
            m = w.dev.mark()
            w.dev.send(f";S0A{t}|;S0B{t}")
            w.dev.expect(rf"^B{t}$", timeout=3, since=m)
            split_ok = f"A{t}" in [x.strip() for x in w.dev.since(m)]
            _sent(w, f";S0C{t}^;S0D{t}", rf"^C{t}\^;S0D{t}$")
            _sent(w, "?config", r"Delimiter Character:\s+\|", timeout=5)
            m = w.dev.mark()
            w.dev.send("WCB_WEBTOOL_CONFIG_PULL")
            w.dev.expect(r"For Configured Boards \(Current Delimiter: '\|'\)", timeout=5, since=m)
            time.sleep(1.5)
            configured, factory = _chains(w.dev.since(m))
            _sent(w, "?DELIM,^", r"^Delimiter updated to: '\^'")
            _sent(w, "?D|", r"^Command delimiter updated to: '\|'")
            _sent(w, "?D^", r"^Command delimiter updated to: '\^'")
        finally:
            _restore_delim(w)
    assert split_ok, "the '|' chain did not run both halves"
    assert "|" in configured and "?DELIM," not in configured and re.search(r"\|\?CHK[0-9A-F]{8}$", configured), configured[-60:]
    assert "?DELIM,|" in factory and re.search(r"\^\?CHK[0-9A-F]{8}$", factory), factory[-60:]


@test("chars.delim_collision_refused", "?DELIM and the legacy ?D<x> refuse either prefix and ',' as the delimiter, and the delimiter stays '^'; ?DELIM,? (the erase-flash lockout) only after the recoverable arms proved the guard (re-scan #7)", needs=["wcb1"], links=[])
def delim_collision_refused(bench):
    """WCB coverage re-scan #7 (docs/hil_plan/WCB.md WCB-WP35 row 1). The delimiter splits every chain before anything
    reads it. As the function identifier it split every function line at its own prefix, the ?DELIM that would undo
    it included, so only an erase-flash recovered the board; as ';' it broke the ';' family; as ',' it split every
    command's arguments and only the legacy ?D^ got the board back. delimCharOk (WCB.ino) now refuses all three in
    both setters. A whole-line setter can never receive the live delimiter itself (the line is split first), so these
    collisions only ever came from changing the delimiter. On a board with the default '?' the lockout was not
    '?DELIM,?' - the trailing-'?' help shortcut printed the help page instead - but '!DELIM,!' with a '!' identifier,
    or ?DELIM,<c> after ?FUNCCHAR,<c>. DELIM is now exempt from that shortcut, so '?DELIM,?' reaches the setter and
    must be refused.
    Order is safety: the recoverable arms go first (a ';' or ',' delimiter is undone by ?DELIM,^ or ?D^), and ?DELIM,?
    is sent only once ?DELIM,; and ?D; were refused. Those prove the check is present in both setters, in the same
    condition as the function-identifier test; should it ever regress there alone, the board needs an erase-flash."""
    w = usb_wcb(bench)
    problems = []
    same = "{} is the {} — every command would split at its own prefix. Pick a different delimiter."
    comma = "',' separates every command's arguments. Pick a different delimiter."

    def refused(line, text):
        m = w.dev.mark()
        w.dev.send(line)
        time.sleep(0.6)
        out = [x.rstrip() for x in w.dev.since(m)]
        ok = text in out
        if not ok:
            problems.append(f"{line} printed {out}")
        if _live_chars(w)[0] != "^":
            problems.append(f"{line} changed the delimiter")
            _restore_delim(w)
            ok = False
        return ok

    with config_guard(bench, 1):
        try:
            gate = refused("?DELIM,;", same.format("';'", "command character"))
            refused("?DELIM,,", comma)
            refused("?DELIM,A", "'A' would split ordinary words. Pick a punctuation character for the delimiter.")
            gate = refused("?D;", same.format("';'", "command character")) and gate
            refused("?D,", comma)
            if gate:
                refused("?DELIM,?", same.format("'?'", "function identifier"))
            else:
                problems.append("?DELIM,? was not sent: the guard is missing from a setter")
        finally:
            _restore_delim(w)
    assert not problems, "; ".join(problems)


@test("chars.funcchar_change_restore", "?FUNCCHAR's collision guard; the '!' prefix works; an old '?' line becomes a broadcast; help-trap-exempt restore", needs=["wcb1"])
def funcchar_change_restore(bench):
    s12 = link(bench, 1, "S2")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON")
    w = usb_wcb(bench)
    t = marker()
    with config_guard(bench, 1):
        guard = [x.rstrip() for x in w.run("?FUNCCHAR,;")]
        try:
            _sent(w, "?FUNCCHAR,!", r"^Local function identifier updated to '!'")
            _sent(w, "!VERSION", r"^End of Version")
            _sent(w, "!CONFIG", r"Local Function Identifier: !", timeout=5)
            pm = s12.mark()
            w.dev.send(f"?HILNOP{t}")             # reaches W2 and NaviCore as 'Unknown command' too
            s12.expect(f"?HILNOP{t}\r".encode(), timeout=3, since=pm)
            m = w.dev.mark()
            w.dev.send("WCB_WEBTOOL_CONFIG_PULL")
            w.dev.expect(r"For Configured Boards", timeout=5, since=m)
            time.sleep(1.5)
            configured, factory = _chains(w.dev.since(m))
            m = w.dev.mark()
            w.dev.send("!LF?")
            time.sleep(1.0)
            helped = w.dev.since(m)
            _sent(w, "!FUNCCHAR,?", r"^Local function identifier updated to '\?'")
            _sent(w, "?VERSION", r"^End of Version")
        finally:
            _, lfi = _live_chars(w)
            if lfi != "?":
                w.dev.send(f"{lfi}FUNCCHAR,?")
                time.sleep(0.5)
    assert "';' is already the command character — the entire ';' command family would stop working. Pick a different function identifier." in guard, guard
    assert configured.startswith("!") and re.search(r"\^!CHK[0-9A-F]{8}$", configured), configured[-60:]
    assert "?FUNCCHAR,!" in factory and re.search(r"\^\?CHK[0-9A-F]{8}$", factory), factory[-60:]
    assert not _has(helped, "updated to"), "!LF? changed the identifier instead of printing help"


@test("chars.legacy_lf_guard", "(should) Legacy ?LF; is refused like ?FUNCCHAR,; instead of making ';' the function identifier", needs=["wcb1"], links=[])
def legacy_lf_guard(bench):
    """Probable bug: legacy ?LF<c> (WCB.ino:5760-5761 -> 5976-5984) sets the LFI with none of the ?FUNCCHAR guards
    (5613-5624), accepting the command character, space and control characters. While the LFI is ';' every ';'
    command lands in processLocalCommand's legacy catch-alls (;M2xx writes the MAC octet), so the window is kept
    under a second and recovered with ;FUNCCHAR,?."""
    w = usb_wcb(bench)
    accepted = False
    with config_guard(bench, 1):
        m = w.dev.mark()
        try:
            w.dev.send("?LF;")
            time.sleep(0.3)
            accepted = _has(w.dev.since(m), "LocalFunctionIdentifier updated to ';'")
        finally:
            if accepted:
                _sent(w, ";FUNCCHAR,?", r"^Local function identifier updated to '\?'")
            w.version()
    assert not accepted, "?LF; made ';' the function identifier"


@test("chars.cmdchar_change_restore", "?CMDCHAR moves the ';' family; the old ';' becomes a broadcast; legacy ?CC also sets it", needs=["wcb1"])
def cmdchar_change_restore(bench):
    s12 = link(bench, 1, "S2")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,OUT,S0,OFF", "?CMDCHAR,;")
    w = usb_wcb(bench)
    t, t2 = marker("a"), marker("b")
    with config_guard(bench, 1):
        try:
            _sent(w, "?CMDCHAR,:", r"^Command character updated to ':'")
            _sent(w, f":S0{t}", rf"^{t}$")
            pm, m = s12.mark(), w.dev.mark()
            w.dev.send(f";S0{t2}")                # a plain broadcast now: it also runs on W2 and reaches NaviCore
            s12.expect(f";S0{t2}\r".encode(), timeout=3, since=pm)
            printed = t2 in [x.strip() for x in w.dev.since(m)]
            _sent(w, "?config", r"Command Character:\s+:", timeout=5)
            _sent(w, "?CMDCHAR,;", r"^Command character updated to ';'")
            _sent(w, "?CC:", r"^CommandCharacter updated to ':'")
            _sent(w, "?CC;", r"^CommandCharacter updated to ';'")
        finally:
            w.dev.send("?CMDCHAR,;")
            time.sleep(0.5)
    assert not printed, "the ';' line still ran locally under ':'"


@test("chars.cmdchar_lfi_guard", "(should) ?CMDCHAR refuses the function identifier, as ?FUNCCHAR refuses the command character", needs=["wcb1"], links=[])
def cmdchar_lfi_guard(bench):
    """Probable gap: ?CMDCHAR (WCB.ino:5635-5644) and legacy ?CC (5986-5994) accept a character equal to the LFI, or
    whitespace: the symmetric collision ?FUNCCHAR rejects (5613-5624). CMDCHAR '?' silently kills the whole ';'
    family (WCB.ino:4883-4889)."""
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        m = w.dev.mark()
        try:
            w.dev.send("?CMDCHAR,?")
            time.sleep(0.4)
            accepted = _has(w.dev.since(m), "Command character updated to '?'")
        finally:
            w.dev.send("?CMDCHAR,;")
            time.sleep(0.5)
    assert not accepted, "?CMDCHAR accepted the function identifier '?'"


# ============================================================ WDP table and controls
def _expected_cap(tokens, n):
    """The CAP bits a WCB should advertise, derived from its config chain (WCB_WDP.cpp:105-123, WCB_WDP.h:40-48)."""
    up = [t.upper() for t in tokens]
    cap = 0
    cap |= 0x0080 if any(re.match(rf"^\?MAESTRO,M\d+:W{n}S\d", t) for t in up) else 0
    cap |= 0x0040 if any(t.startswith("?CONTROLLER,ON") for t in up) else 0
    cap |= 0x0020 if any(t.startswith("?MAP,PWM") for t in up) else 0
    cap |= 0x0010 if "?MAESTRO,REMOTE" in up else 0
    cap |= 0x0008 if any(t.startswith("?KYBER,LOCAL") for t in up) else 0   # the backup writes ?KYBER,LOCAL,S<n>[,targets]
    cap |= 0x0004 if any(re.match(rf"^\?WLED,\d+:W{n}S\d", t) for t in up) else 0
    cap |= 0x0001 if any(t.startswith("?HCR,PORT") for t in up) else 0
    cap |= 0x0002 if any(re.match(r"^\?MP3,S\d", t) for t in up) else 0
    cap |= 0x0100 if any(re.match(r"^\?DFP,S\d", t) for t in up) else 0
    return cap


@test("wdp.dump_fields", "?WDP,DUMP rows for W1 (self), W2 and NaviCore match their configuration", needs=["wcb1"], links=[])
def dump_fields(bench):
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    t1, t2 = snapshot(bench, me), snapshot(bench, 2)
    fw = w.version()
    dump = [x for x in _dump(w) if x.startswith("[WDP")]
    self_row, row2, row20 = _row(dump, me), _row(dump, 2), _row(dump, 20)
    bad = []
    if not dump or dump[0] != self_row:
        bad.append("the self row is not first")
    for name, want in (("CLIENT", "0"), ("FW", fw), ("CAP", "%04X" % _expected_cap(t1, me)), ("AGE", "0"), ("SEEN", "1"), ("PEER", "3"),
                       ("CTRL", "20" if "?CONTROLLER,ON,20" in t1 else _field(self_row, "CTRL"))):
        if _field(self_row, name) != want:
            bad.append(f"self {name}={_field(self_row, name)}, expected {want}")
    for t in t1:
        m = re.match(r"^\?LABEL,S(\d),(.+)$", t)
        if m and f"[WDPIF:N={me},S={m.group(1)},DEV={m.group(2)[:24]}]" not in dump:
            bad.append(f"no WDPIF row for W{me} S{m.group(1)}")
    for n in (me, 2):
        if not any(x.startswith(f"[WDPX:N={n},") for x in dump) or not any(re.match(rf"^\[WDPSEQ:N={n},HASH=[0-9A-F]{{8}}\]$", x) for x in dump):
            bad.append(f"W{n} lacks its WDPX or WDPSEQ row")
    if row2 is None:
        bad.append("no W2 row")
    else:
        for name, want in (("CLIENT", "0"), ("CAP", "%04X" % _expected_cap(t2, 2)), ("SEEN", "1"), ("PEER", "1")):
            if _field(row2, name) != want:
                bad.append(f"W2 {name}={_field(row2, name)}, expected {want}")
        if int(_field(row2, "AGE") or 999) > 75:
            bad.append(f"W2 AGE {_field(row2, 'AGE')}")
        if _field(row2, "FW") != fw:
            bench.note(f"W2 runs {_field(row2, 'FW')}, W1 runs {fw}")
    if row20 is not None:
        for name, want in (("CLIENT", "1"), ("CAP", "0000"), ("CTRL", "0"), ("PEER", "0")):
            if _field(row20, name) != want:
                bad.append(f"NaviCore {name}={_field(row20, name)}, expected {want}")
    k = sum(1 for x in dump if x.startswith("[WDP:N=") and x != self_row)
    if len(dump) < 2 or not dump[-2].startswith("[WDPCFG:") or dump[-1] != f"[WDP:END,count={k}]":
        bad.append(f"last two lines {dump[-2:]}, expected WDPCFG then END count={k}")
    assert not bad, "; ".join(bad)


@test("wdp.list_detail_errors", "?WDP / ?WDP,LIST table, the detail views, and the error lines", needs=["wcb1"], links=[])
def list_detail_errors(bench):
    w = usb_wcb(bench)
    bad = []
    if _row(_dump(w), 19):
        raise Skip("a neighbour WCB19 exists")
    for cmd in ("?WDP", "?WDP,LIST"):
        out = [x.rstrip() for x in w.run(cmd)]
        if "Capability codes: M=Maestro host  R=Maestro remote  K=Kyber  H=HCR  3=MP3  W=WLED  P=PWM  C=Controller link  D=DFPlayer" not in out:
            bad.append(f"{cmd}: no capability legend")
        if not any(re.match(r"^Total WDP neighbors: \d+   \(\?WDP,<n> for detail\)$", x) for x in out):
            bad.append(f"{cmd}: no total line")
        if not any(x.startswith("2   ") and x.endswith("live") for x in out):
            bad.append(f"{cmd}: no live row for WCB2")
    d2 = [x.rstrip() for x in w.run("?WDP,2")]
    if not any(x.startswith('==== WCB 2  "') and x.endswith('" ====') for x in d2) or not any(x.startswith("  Platform    : ") for x in d2):
        bad.append(f"?WDP,2 detail {d2[:4]}")
    d20 = [x.rstrip() for x in w.run("?WDP,DETAIL,20")]
    if _row(_dump(w), 20) and not any(x.startswith("==== Device 20  ") for x in d20):
        bad.append(f"?WDP,DETAIL,20 {d20[:3]}")
    if not _has(w.run("?WDP,19"), "[WDP] no neighbor WCB19 — see ?WDP,LIST"):
        bad.append("?WDP,19")
    # Never probe unknown subcommands starting ADD/FORGET/CLEAR/AUTOJOIN/DETAIL/DA, or a digit: they are prefix-matched.
    if not _has(w.run("?WDP,FOO"), "[WDP] unknown subcommand 'FOO' (LIST | <n> | DETAIL,n | STATUS | DUMP | DA | DA,FORGET,S<n>[,<type>] | DA,CLEAR | POLL | ON | OFF | AUTOJOIN[,ON|,OFF] | ADD,<id> | FORGET,<id> | CLEAR)"):
        bad.append("?WDP,FOO")
    assert not bad, "; ".join(bad)


@test("wdp.poll_refreshes_age", "?WDP,POLL makes W2 and NaviCore re-advertise within about a second (up to ~75 s)", needs=["wcb1"], links=[])
def poll_refreshes_age(bench):
    w = usb_wcb(bench)
    deadline = time.monotonic() + 70
    while True:
        dump = _dump(w)
        ages = [int(_field(_row(dump, n), "AGE") or 0) for n in (2, 20) if _row(dump, n)]
        if ages and min(ages) >= 5:
            break
        if time.monotonic() > deadline:
            raise Skip("neighbour ages never reached 5 s (something keeps advertising)")
        time.sleep(2)

    def late_after_poll(boards):
        poll = w.run("?WDP,POLL")
        time.sleep(3)
        dump = _dump(w)
        assert _has(poll, "[WDP] polled: advertised + solicited the mesh"), poll
        return {n: _field(_row(dump, n), "AGE") for n in boards if _row(dump, n) and int(_field(_row(dump, n), "AGE")) > 3}

    late = late_after_poll((2, 20))
    if late:
        # The SOLICIT (WCB_WDP.cpp:1875-1876) and each board's one answering advert are single unacknowledged ESP-NOW
        # broadcasts with no MAC-layer retry, so losing one frame is legitimate: in 20260924-190733 NaviCore's answer
        # (or the SOLICIT to it) was lost while W2's came back at AGE 2, and the rerun passed. Poll once more, as
        # wdp.controller_auto_enable does (tracker #68). A missed board reads AGE ~11 by now, so two misses in a row fail.
        bench.note(f"first ?WDP,POLL not answered by {sorted(late)} (AGE {late}); polling once more")
        late = late_after_poll(late)
    assert not late, f"not refreshed by two polls in a row: {late}"


@test("wdp.off_on", "?WDP,OFF ignores adverts, refuses POLL and rides the config chain; aliases still resolve; ON restores", needs=["wcb1"])
def off_on(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    t = marker()
    bad = []
    with config_guard(bench, 1):
        alias2 = _field(_row(_dump(w), 2), "ALIAS")
        if not alias2:
            raise Skip("W2 has no alias in W1's table")
        try:
            if not _has(w.run("?WDP,OFF"), "[WDP] disabled"):
                bad.append("OFF did not confirm")
            dump = _dump(w)
            a0 = int(_field(_row(dump, 2), "AGE"))
            if not _has(dump, "[WDPCFG:EN=0,"):
                bad.append("WDPCFG does not show EN=0")
            if not _has(w.run("?WDP,POLL"), "[WDP] POLL needs WDP + ETM enabled"):
                bad.append("POLL did not refuse")
            if "?WDP,OFF" not in snapshot(bench, 1):
                bad.append("the chain lacks ?WDP,OFF")
            w.send(";W2,?WDP,POLL")                  # W2 adverts; W1 must ignore them
            time.sleep(8)
            a1 = int(_field(_row(_dump(w), 2), "AGE"))
            if a1 < a0 + 8:
                bad.append(f"W2's AGE went {a0} -> {a1}: W1 accepted an advert while off")
            pm = s2.mark()
            w.send(f";W{alias2},;S2{t}")
            try:
                s2.expect(t.encode() + b"\r", timeout=3, since=pm)
            except AssertionError:
                bad.append("the alias stopped resolving while WDP was off")
        finally:
            on = w.run("?WDP,ON")
            poll = w.run("?WDP,POLL")
    assert _has(on, "[WDP] enabled") and _has(poll, "[WDP] polled: advertised + solicited the mesh"), "ON / POLL after"
    assert not bad, "; ".join(bad)


@test("wdp.autojoin_query_set", "?WDP,AUTOJOIN query, set and usage; the chain token; the ?PEERSLIVE suffix", needs=["wcb1"], links=[])
def autojoin_query_set(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1):
        if not _has(w.run("?WDP,AUTOJOIN"), "[WDP] auto-join is ON"):
            raise Skip("auto-join is not ON")
        try:
            if not _has(w.run("?WDP,AUTOJOIN,OFF"), "[WDP] auto-join disabled"):
                bad.append("OFF did not confirm")
            if not re.match(r"^Live peers: \d+ \(WCBQ floor \d+\)$", _live_peers(w)[1]):
                bad.append(f"?PEERSLIVE kept the auto-join suffix: {_live_peers(w)[1]}")
            if not _has(_dump(w), "[WDPCFG:EN=1,AUTOJOIN=0,"):
                bad.append("WDPCFG does not show AUTOJOIN=0")
            if "?WDP,AUTOJOIN,OFF" not in snapshot(bench, 1):
                bad.append("the chain lacks ?WDP,AUTOJOIN,OFF")
            if not _has(w.run("?WDP,AUTOJOIN,MAYBE"), "[WDP] usage: ?WDP,AUTOJOIN[,ON|,OFF]"):
                bad.append("MAYBE did not print usage")
        finally:
            on = w.run("?WDP,AUTOJOIN,ON")
    assert _has(on, "[WDP] auto-join enabled"), on
    assert not bad, "; ".join(bad)


@test("wdp.add_forget_noop_ids", "?WDP,ADD / FORGET on self, controller, floor and invalid ids leave no state", needs=["wcb1"], links=[])
def add_forget_noop_ids(bench):
    """Never ?WDP,ADD an id above WCBQ: it persists a learned peer the backup does not show."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    tokens = snapshot(bench, me)
    if token(tokens, "?WCBQ,") != "?WCBQ,2":
        raise Skip("W1's WCBQ is not 2")
    n, line = _live_peers(w)
    checks = [(f"?WDP,ADD,{me}", f"[WDP] could not add WCB{me}"), ("?WDP,ADD,0", "[WDP] usage: ?WDP,ADD,<id>"),
              ("?WDP,ADD", "[WDP] usage: ?WDP,ADD,<id>"), ("?WDP,FORGET,0", "[WDP] usage: ?WDP,FORGET,<id>")]
    if "?CONTROLLER,ON,20" in tokens:
        checks.append(("?WDP,ADD,20", "[WDP] could not add WCB20"))
    bad = [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
    out = w.run("?WDP,ADD,2")
    if not _has(out, "[WDP] added WCB2 as a peer") or _has(out, "[PEER]"):
        bad.append(f"?WDP,ADD,2 (a floor peer) printed {out}")
    if _live_peers(w)[1] != line:
        bad.append(f"live peers changed: {line} -> {_live_peers(w)[1]}")
    assert not bad, "; ".join(bad)


@test("wdp.forget_floor_relearn", "?WDP,FORGET,2 drops the neighbour row (alias routing fails) but not floor membership; POLL relearns", needs=["wcb1"])
def forget_floor_relearn(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    m1, m2, m3 = marker("a"), marker("b"), marker("c")
    bad = []
    alias2 = _field(_row(_dump(w), 2), "ALIAS")
    if not alias2:
        raise Skip("W2 has no alias in W1's table")
    try:
        out = w.run("?WDP,FORGET,2")
        if not _has(out, "[WDP] forgot WCB2") or _has(out, "[PEER] WCB2 unregistered."):
            bad.append(f"FORGET printed {out}")
        if _row(_dump(w), 2):
            bad.append("the W2 row survived FORGET")
        pm = s2.mark()
        if not _has(w.run(f";W{alias2},;S2{m1}"), f'No board named "{alias2}" is known'):
            bad.append("the alias still resolved after FORGET")
        m = s2.mark()
        w.send(f";W2,;S2{m2}")
        try:
            s2.expect(m2.encode() + b"\r", timeout=3, since=m)
        except AssertionError:
            bad.append("W2 stopped being reachable by number (floor peer)")
        wm = w.dev.mark()
        w.run("?WDP,POLL")
        try:
            w.dev.expect(r"\[WDP\] learned WCB2", timeout=3, since=wm)
        except AssertionError:
            bad.append("POLL did not relearn W2")
        time.sleep(1)
        m = s2.mark()
        w.send(f";W{alias2},;S2{m3}")
        try:
            s2.expect(m3.encode() + b"\r", timeout=3, since=m)
        except AssertionError:
            bad.append("the alias did not resolve after the relearn")
        if _field(_row(_dump(w), 2), "PEER") != "1":
            bad.append("the relearned row is not PEER=1")
        if m1.encode() in s2.received(pm):
            bad.append("the unresolvable alias still delivered")
    finally:
        if not _row(_dump(w), 2):
            w.run("?WDP,POLL")
        time.sleep(6)                 # FORGET schedules a learned-peers NVS flush 5 s later (WCB.ino:525)
    assert not bad, "; ".join(bad)


# ============================================================ the probe as a temporary mesh client (slow; run last)
EVICTED = r"\[PEER\] temporary WCB15 evicted — silent for \d+s"


def _require_id_free(w, n=15):
    if _row(_dump(w), n) or any(re.match(rf"^  WCB{n}: ", x) for x in _cfg(w)):
        raise Skip(f"mesh id {n} is in use")


@test("wdp.temp_probe_lifecycle", "Probe as a TEMPORARY client: joined and reachable, then silent (unicast and broadcast fail), evicted ~50 s after leaving, never OFFLINE (slow, ~90 s)", needs=["wcb1", "probe2"], links=[])
def temp_probe_lifecycle(bench):
    """Eviction is 50 s of silence (TEMPORARY_PEER_TTL_MS, WCB.ino:508), before the 55 s offline threshold, so a
    temporary peer never prints 'went OFFLINE'. Until it is evicted every W1/W2 broadcast expects its ACK."""
    w = usb_wcb(bench)
    _require_id_free(w)
    n, _ = _live_peers(w)
    t, t1, t2 = marker("a"), marker("b"), f"hilbc{nonce().lower()}"
    bad = []
    w.run("?DEBUG,ETM,ON")
    try:
        wm = w.dev.mark()
        with probe_in_mesh(bench, "probe2", 15, forget=False) as probe:
            w.dev.expect(r"\[ETM\] WCB15 came ONLINE", timeout=20, since=wm)
            joined = [x.rstrip() for x in w.dev.since(wm)]
            steps = ["[WDP] learned WCB15 HILProbe", "[PEER] WCB15 registered (live, TEMPORARY).",
                     "[WDP] temporarily joined WCB15 HILProbe (temporary)", "[ETM] WCB15 came ONLINE"]
            idx = [next((i for i, x in enumerate(joined) if s in x), -1) for s in steps]
            if -1 in idx or idx != sorted(idx):
                bad.append(f"join lines missing or out of order: {dict(zip(steps, idx))}")
            row = _row(_dump(w), 15)
            bad += [f"row {k}={_field(row, k)}" for k, v in (("CLIENT", "1"), ("ALIAS", "HILProbe"), ("FW", "wcb_probe-2"),
                                                             ("CAP", "0000"), ("PEER", "4")) if _field(row, k) != v]
            if _live_peers(w)[0] != n + 1:
                bad.append("live peers did not grow by one")
            pm = probe.dev.mark()
            w.send(f";W15,{t}")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any(s == 1 and x.startswith(t) for s, x in probe.mesh_received(pm)):
                time.sleep(0.1)
            if not any(s == 1 and x.startswith(t) for s, x in probe.mesh_received(pm)):
                bad.append("the probe did not receive W1's unicast")
            w.run("?STATS,RESET")
            t_leave, lm = time.monotonic(), w.dev.mark()
            probe.mesh_leave()                      # reboots the probe: from here it is silent
        sm = w.dev.mark()
        w.send(f";W15,{t1}")
        w.send(t2)
        time.sleep(3)
        lines = [x.rstrip() for x in w.dev.since(sm)]
        stats = [x.rstrip() for x in w.run("?STATS")]
        try:
            w.dev.expect(EVICTED, timeout=max(1.0, t_leave + 60 - time.monotonic()), since=lm)
            evicted_after = round(next(ts for ts, x in _timed(w.dev, lm) if "temporary WCB15 evicted" in x) - t_leave, 1)
        except AssertionError:
            evicted_after = None
        after, dump2 = [x.rstrip() for x in w.dev.since(lm)], _dump(w)
        live2, unreachable = _live_peers(w)[0], w.run(";W15,x")
    finally:
        w.run("?DEBUG,ETM,OFF")
    bench.note(f"temporary peer evicted {evicted_after} s after MESH LEAVE")
    for label, text in (("unicast", t1), ("broadcast", t2)):
        if not any(re.search(rf"\[ETM\] WCB15 failed to ACK seq \d+ after 3 retries: {text}", x) for x in lines):
            bad.append(f"no WCB15 failure line for the {label}")
    if not any(re.match(r"^WCB15: Sent: 2, ACKd: 0, Retries: 6, Failed: 2", x) for x in stats):
        bad.append(f"stats {[x for x in stats if x.startswith('WCB15: ')]}")
    if evicted_after is None or not 38 <= evicted_after <= 53:
        bad.append(f"eviction {evicted_after} s after leaving (expected 38-53)")
    if _has(after, "[ETM] WCB15 went OFFLINE"):
        bad.append("the temporary peer went OFFLINE before eviction")
    if _row(dump2, 15) or live2 != n:
        bad.append("the row or the live-peer count survived eviction")
    if not _has(unreachable, "WCB 15 is not a reachable target — it isn't a configured or learned peer or the controller."):
        bad.append(";W15 still reachable")
    assert not bad, "; ".join(bad)


@test("wdp.autojoin_off_ignores_probe", "With auto-join off a temporary probe is learned but not joined; turning auto-join on joins it at its next advert (slow, ~95 s)", needs=["wcb1", "probe2"], links=[])
def autojoin_off_ignores_probe(bench):
    w = usb_wcb(bench)
    _require_id_free(w)
    bad = []
    joined = evicted = False
    with config_guard(bench, 1):
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            wm = w.dev.mark()
            with probe_in_mesh(bench, "probe2", 15, forget=False):
                time.sleep(5)                        # the probe's 3-advert boot burst
                early = [x.rstrip() for x in w.dev.since(wm)]
                row, unreachable = _row(_dump(w), 15), w.run(";W15,x")
                jm = w.dev.mark()
                on = w.run("?WDP,AUTOJOIN,ON")
                try:
                    # A temporary client's first periodic advert is WCB_WDP_TEMP_ADVERT_MS + (id % 16) * 500 ms
                    # after it sets its identity (WCB_Client.cpp:1889), so id 15 adverts 22.5 s after joining —
                    # about 16 s after AUTOJOIN,ON here, and every 15 s after that (:1994-1999).
                    w.dev.expect(r"\[WDP\] temporarily joined WCB15", timeout=30, since=jm)
                    joined = True
                except AssertionError:
                    pass
                later = [x.rstrip() for x in w.dev.since(jm)]
                lm = w.dev.mark()
            if joined:
                try:
                    w.dev.expect(EVICTED, timeout=60, since=lm)
                    evicted = True
                except AssertionError:
                    pass
        finally:
            w.run("?WDP,AUTOJOIN,ON")
            if _row(_dump(w), 15):
                w.run("?WDP,FORGET,15")          # the RAM row of a probe that left before it was joined
    if not _has(early, "[WDP] learned WCB15") or _has(early, "temporarily joined") or _has(early, "[PEER] WCB15"):
        bad.append(f"with auto-join off: {[x for x in early if 'WCB15' in x]}")
    if _field(row, "PEER") != "0":
        bad.append(f"row PEER={_field(row, 'PEER')} with auto-join off")
    if not _has(unreachable, "WCB 15 is not a reachable target"):
        bad.append(";W15 reachable with auto-join off")
    if not _has(on, "[WDP] auto-join enabled"):
        bad.append("AUTOJOIN,ON did not confirm")
    if not joined or not _has(later, "[PEER] WCB15 registered (live, TEMPORARY)."):
        bad.append("the probe was not joined at its next advert")
    if joined and not evicted:
        bad.append("no eviction within 60 s of leaving")
    assert not bad, "; ".join(bad)


@test("etm.rx_crc_gate_probe", "W1's receive CRC gate: a missing or wrong |CRC suffix is rejected and a correct one runs; W1's own suffix format (~40 s)", needs=["wcb1", "probe2"])
def rx_crc_gate_probe(bench):
    """The probe joins with checksums off, so it sends and receives the suffix verbatim (WCB_Client.cpp:2303-2309).
    W1 ACKs all three before its CRC check (WCB.ino:4269-4272), so the probe's ensured sends all look delivered.
    Id 14: this test sends commands, so its id must be fresh in W1's duplicate ring."""
    s12 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?ETM,CHKSM,ON")
    _require_id_free(w, 14)
    m1, m2, m3, m4 = (marker(x) for x in "abcd")

    def crc(text):
        return "%08X" % (zlib.crc32(text.encode()) & 0xFFFFFFFF)

    results = {}
    try:
        w.run("?DEBUG,ETM,ON")
        wm = w.dev.mark()
        with probe_in_mesh(bench, "probe2", 14, checksum=False) as probe:
            w.dev.expect(r"\[ETM\] WCB14 came ONLINE", timeout=20, since=wm)
            for key, text in (("m1", f";S2{m1}"), ("m2", f";S2{m2}|CRC00000000"), ("m3", f";S2{m3}|CRC{crc(';S2' + m3)}")):
                pm, cm = s12.mark(), w.dev.mark()
                probe.mesh_send(1, text)
                time.sleep(2)
                results[key] = (s12.received(pm), [x.rstrip() for x in w.dev.since(cm)])
            pm = probe.dev.mark()
            w.send(f";W14,{m4}")
            time.sleep(2)
            rx = probe.mesh_received(pm)
    finally:
        w.run("?DEBUG,ETM,OFF")
    (g1, l1), (g2, l2), (g3, l3) = results["m1"], results["m2"], results["m3"]
    assert any(re.search(r"\[ETM\] seq \d+ rejected: missing CRC", x) for x in l1) and m1.encode() not in g1, "missing CRC not rejected"
    assert any(re.search(rf"\[ETM\] seq \d+ rejected: CRC mismatch \(rx 00000000 calc {crc(';S2' + m2)}\)", x) for x in l2) \
        and m2.encode() not in g2, "wrong CRC not rejected"
    assert any(re.search(rf"\[ETM\] Received seq \d+ from WCB14: ;S2{m3}", x) for x in l3) and m3.encode() + b"\r" in g3, "correct CRC did not run"
    assert any(s == 1 and x == f"{m4}|CRC{crc(m4)}" for s, x in rx), f"W1's suffix format: {rx}"


@test("wdp.da_announce_propagates", "An @WDP1 announce on an unlabelled W2 port reaches W1's DUMP through W2's on-change advert and device list, goes quiet after 90 s but stays, and a forget clears it (slow, ~100 s)", needs=["wcb1"], links=["W2S3|W2S4|W2S5"])
def da_announce_propagates(bench):
    w = usb_wcb(bench)
    tokens2 = snapshot(bench, 2)
    port = next((p for p in ("S4", "S3", "S5") if bench.links.usable(2, p, send=True) and not token(tokens2, f"?LABEL,{p},")), None)
    if port is None:
        raise Skip("no wired, unlabelled W2 port among S3-S5")
    l = link(bench, 2, port)
    s = port[1]
    with Console(bench, 2) as c2:
        _crun(c2, f"?WDP,DA,FORGET,{port},HILDev")   # devices are kept until forgotten: clear a killed run's copy
        try:
            cm = c2.mark()
            l.send(b'@WDP1 {"type":"HILDev","fw":"9.9"}\r\n')
            time.sleep(0.5)
            l.send(b'@WDP1 {"type":"HILDev","fw":"9.9"}\r\n')   # the second announce saves it, and it is advertised
            sent_at = time.monotonic()
            time.sleep(3)
            announce, dump = c2.lines(cm), _dump(w)
            da = _crun(c2, "?WDP,DA", 1.0)
            time.sleep(max(0.0, 95 - (time.monotonic() - sent_at)))
            stopped_after = next((round(ts - sent_at, 1) for ts, x in c2.dev.lines[cm:] if f"[WDP-DA] {port}: HILDev stopped announcing" in x), None)
            time.sleep(2)                              # going quiet re-sends W2's device list
            dump2 = _dump(w)
            _crun(c2, f"?WDP,DA,FORGET,{port},HILDev")
            time.sleep(3)
            dump3 = _dump(w)
        finally:
            _crun(c2, f"?WDP,DA,FORGET,{port},HILDev")
    bench.note(f"WDP-DA device on W2 {port} went quiet {stopped_after} s after its announce")
    assert _has(announce, f"[WDP-DA] {port}: HILDev fw 9.9"), "W2 did not log the announce"
    assert f"[WDPIF:N=2,S={s},DEV=HILDev]" in dump, "W1's DUMP lacks the announced device"
    assert f"[WDPDA:N=2,S={s},TYPE=HILDev,FW=9.9,HW=,CAPS=,SEEN=1,AGE=-]" in dump, "W1's DUMP lacks W2's device record"
    assert _has(da, "Serial-attached devices (WDP-DA announces):") and any(x.strip().startswith(f"{port}  HILDev") and "fw 9.9" in x for x in da), da
    assert stopped_after is not None and 88 <= stopped_after <= 94, f"'stopped announcing' after {stopped_after} s (expected ~90-92)"
    assert f"[WDPIF:N=2,S={s},DEV=HILDev]" in dump2, "the quiet device stopped naming W2's port in W1's DUMP"
    assert f"[WDPDA:N=2,S={s},TYPE=HILDev,FW=9.9,HW=,CAPS=,SEEN=0,AGE=-]" in dump2, "W1 did not see the device go quiet"
    assert not any(x.startswith(f"[WDPIF:N=2,S={s},") for x in dump3), "the forgotten device still names W2's port in W1's DUMP"
    assert not any(x.startswith(f"[WDPDA:N=2,S={s},TYPE=HILDev,") for x in dump3), "W1 still lists the forgotten device"


# ============================================================ legacy spellings and read-only status
@test("chars.legacy_debug_toggles", "Legacy ?DON/?DOFF, ?DMON/?DMOFF (= ?DKON/?DKOFF), ?DPWMON/OFF, ?DETMON/OFF and ?DHCRON/OFF flip the RAM debug flags and print their lines", needs=["wcb1"], links=[])
def legacy_debug_toggles(bench):
    """WCB.ino's legacy block ('don' .. 'dhcron'). RAM only: nothing reaches NVS, and every flag is put back OFF."""
    w = usb_wcb(bench)
    checks = [("?DON", "Debugging enabled"), ("?DOFF", "Debugging disabled"),
              ("?DMON", "Maestro debugging enabled"), ("?DMOFF", "Maestro debugging disabled"),
              ("?DKON", "Maestro debugging enabled"), ("?DKOFF", "Maestro debugging disabled"),
              ("?DPWMON", "PWM debugging enabled"), ("?DPWMOFF", "PWM debugging disabled"),
              ("?DETMON", "ETM debugging enabled"), ("?DETMOFF", "ETM debugging disabled"),
              ("?DHCRON", "HCR debugging enabled"), ("?DHCROFF", "HCR debugging disabled"),
              ("?don", "Debugging enabled"), ("?doff", "Debugging disabled")]
    bad = []
    try:
        for cmd, want in checks:
            out = w.run(cmd)
            if not _has(out, want):
                bad.append(f"{cmd}: {out}")
    finally:
        for cmd in ("?DOFF", "?DMOFF", "?DPWMOFF", "?DETMOFF", "?DHCROFF"):
            w.run(cmd)
    assert not bad, "; ".join(bad)


@test("wifi.status", "?WIFI prints the status block for the board's mode (OFF, AP or JOIN): mode and interface lines, WS endpoint, radio channel = mesh channel, free heap", needs=["wcb1"], links=[])
def wifi_status(bench):
    """wcbWifiPrintStatus (WCB_WiFi.cpp). Read-only, so it runs in whatever mode the board is in (W1 hosts an AP on the
    bench); the mode changes are wifi.off_and_back and wifi.join_w2_ap (opt-in wifi_modes) and the WebSocket endpoint
    wifi.pc_joins_ap_ws (opt-in wifi_pc, attended), all in s28_wifi.py. The ?WIFI line is emitted with the credentials and is not in the chain the harness
    compares, so the mode is read from the block itself. The SSID is printed, the password only as 'set'."""
    w = usb_wcb(bench)
    ch = (token(bench.config_tokens(1), "?WCBCH,") or "?WCBCH,?").split(",")[1]
    out = [x.rstrip() for x in w.run("?WIFI")]
    mode = next((x for x in out if x.startswith("Mode          : ")), None)
    assert mode, f"?WIFI printed no status block: {out[:6]}"
    bad = [f"no {x!r}" for x in ("------ WiFi ------------------------------------------",
                                 "------------------------------------------------------") if x not in out]
    up = "Interface     : up" in out
    if not up and "Interface     : down" not in out:
        bad.append("no Interface line")
    if mode == "Mode          : OFF (ESP-NOW only)":
        if up or "WS endpoint   : NOT RUNNING" not in out:
            bad.append("WiFi off, yet the interface is up or a WS endpoint is running")
    elif mode == "Mode          : AP":
        if not any(x.startswith("AP SSID       : ") for x in out):
            bad.append("no AP SSID line")
        if not any(x in ("AP password   : set", "AP password   : NOT SET — AP will not start") for x in out):
            bad.append("no AP password line")
        if up and not (any(x.startswith("Clients       : ") for x in out) and any(x.startswith("IP address    : ") for x in out)):
            bad.append("AP up, but no Clients / IP address line")
    elif mode == "Mode          : JOIN":
        if not any(x.startswith("Join SSID     : ") for x in out) or not any(re.match(r"^Association   : (not )?connected \(\d+ attempt\(s\)\)$", x) for x in out):
            bad.append("no Join SSID / Association lines")
    else:
        bad.append(f"unknown mode line {mode!r}")
    radio = next((x for x in out if x.startswith("Radio channel : ")), "")
    m = re.match(r"^Radio channel : (\d+)  \(mesh channel (\d+)\)(.*)$", radio)
    if not m or m.group(2) != ch:
        bad.append(f"radio line {radio!r} (config ?WCBCH,{ch})")
    elif m.group(3):
        bad.append(f"the radio is off the mesh channel, so the mesh is dead: {radio!r}")
    if not any(re.match(r"^WS endpoint   : (ws://\S+/ws  \(\d+ client\(s\) connected\)|NOT RUNNING)$", x) for x in out):
        bad.append("no WS endpoint line")
    if not any(re.match(r"^Free heap     : \d+ bytes \(min since boot \d+\)$", x) for x in out):
        bad.append("no Free heap line")
    bench.note("W1 WiFi: " + "; ".join(x.strip() for x in out if x.startswith(("Mode", "Interface", "WS endpoint", "Radio channel"))))
    assert not bad, "; ".join(bad)


# ============================================================ coverage re-scan: WCB-WP35 rows 2-4, WP48, WP30, WP19
# docs/hil_plan/WCB.md. Each docstring names its work-package row. None of these changes ?MAC, ?EPASS, ?WCB or ?WCBCH.
_PRINTABLE = "Prefix characters must be printable, non-space characters."
# WCB_WDP.cpp WDP_BAUD_TABLE: a baud with no code is shown as a bare id (wdpIdBaudStr, printWdpDetail).
_WDP_BAUDS = (0, 110, 300, 600, 1200, 2400, 9600, 14400, 19200, 38400, 57600, 115200, 128000, 256000)
# wdpCapCodes / wdpCapNames (WCB_WDP.cpp), in their print order.
_CAPS = ((0x0080, "M", "Maestro host"), (0x0010, "R", "Maestro remote"), (0x0008, "K", "Kyber local"),
         (0x0001, "H", "HCR"), (0x0002, "3", "MP3"), (0x0004, "W", "WLED"), (0x0020, "P", "PWM"),
         (0x0040, "C", "Controller link"), (0x0100, "D", "DFPlayer"))


def _prefix_chars(w):
    """(delimiter, function identifier, command character), read with WCB_WEBTOOL_CONFIG_PULL, which is recognised
    whatever they are (handleSingleCommand, WCB.ino). The command character is the live chain's CMDCHAR token, which
    collectConfigCommands emits in both chains."""
    m = w.dev.mark()
    w.dev.send("WCB_WEBTOOL_CONFIG_PULL")
    delim = w.dev.expect(r"For Configured Boards \(Current Delimiter: '(.)'\)", timeout=5, since=m).group(1)
    time.sleep(1.5)
    chain = _chains(w.dev.since(m))[0]
    cc = next((t[9:10] for t in chain.split(delim) if t[1:9].upper() == "CMDCHAR,"), ";")
    return delim, chain[:1] or "?", cc


def _restore_prefixes(w):
    """Put the delimiter, function identifier and command character back to ^ ? ; with dev.send (WCB.run needs them)."""
    delim, lfi, cc = _prefix_chars(w)
    if delim != "^":
        w.dev.send(f"{lfi}D^" if delim == "," else f"{lfi}DELIM,^")     # the spelling must not contain the live delimiter
        time.sleep(0.5)
    if lfi != "?":
        w.dev.send(f"{lfi}FUNCCHAR,?")                                    # e.g. '\x7fFUNCCHAR,?' after a DEL identifier
        time.sleep(0.5)
    if cc != ";":
        w.dev.send("?CMDCHAR,;")
        time.sleep(0.5)


def _peers_back(w, wait=20):
    """After a W1 reboot: ?WDP,POLL and wait until ?STATS shows W2 online again, so the next test finds the bench as it
    was (docs/HIL_TESTING.md §6: a board that has just booted sees every peer offline until its next packet). Cleanup:
    never raises. -> True once W2 is online."""
    deadline = time.monotonic() + wait
    try:
        w.run("?WDP,POLL")
        while time.monotonic() < deadline:
            if any(x.startswith("WCB2: ") and "Online" in x for x in w.run("?STATS")):
                return True
            time.sleep(2)
    except AssertionError:
        pass
    return False


def _etm_row(stats, n, special=False):
    """(sent, ackd, retries, failed, online) from ?STATS' ETM row for WCB<n> - 'WCB<n> (special): ' for the controller
    (buildStatsString, WCB.ino) - or None."""
    rx = re.compile(r"^WCB%d%s: Sent: (\d+), ACKd: (\d+), Retries: (\d+), Failed: (\d+), (Online|OFFLINE)"
                    % (n, re.escape(" (special)") if special else ""))
    m = next((m for x in stats for m in [rx.match(x.strip())] if m), None)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5) == "Online") if m else None


def _stats(w):
    return [x.rstrip() for x in w.run("?STATS")]


@test("chars.format_refusals", "?DELIM / ?FUNCCHAR / ?CMDCHAR with anything but one character, the bare legacy ?LF and ?CC, and a DEL or control byte offered as a prefix or delimiter are each refused with their own line; the characters, ?config and the chain are unchanged", needs=["wcb1"], links=[])
def format_refusals(bench):
    """WCB-WP35 row 2. processLocalCommand's DELIM / FUNCCHAR / CMDCHAR branches print 'Invalid format. Use: ?<VERB>,x
    where x is one character'; the legacy branch hands a bare ?LF / ?CC (three characters at most) to
    updateLocalFunctionIdentifier / updateCommandCharacter, which print 'Invalid LocalFunctionIdentifier update command'
    / 'Invalid CommandCharacter update command'; prefixCharOk and delimCharOk refuse a byte <= ' ' or DEL (all
    WCB.ino). A space never reaches them from a console: processIncomingSerial trims every line, so '?CMDCHAR, '
    arrives as '?CMDCHAR,' and gets the format line. Sent with dev.send and read after a pause, because WCB.run() stops
    working if a refusal regresses; a regression is put back at once (_restore_prefixes)."""
    w = usb_wcb(bench)
    fmt = "Invalid format. Use: ?{},x where x is one character"
    checks = [("?DELIM,ab", fmt.format("DELIM")), ("?FUNCCHAR,", fmt.format("FUNCCHAR")),
              ("?CMDCHAR,xy", fmt.format("CMDCHAR")), ("?LF", "Invalid LocalFunctionIdentifier update command"),
              ("?CC", "Invalid CommandCharacter update command"), ("?CMDCHAR,\x7f", _PRINTABLE),
              ("?FUNCCHAR,\x7f", _PRINTABLE), ("?CC\x7f", _PRINTABLE), ("?CMDCHAR,\x01", _PRINTABLE),
              ("?LF\x01", _PRINTABLE), ("?DELIM,\x7f", "The delimiter must be a printable, non-space character.")]
    problems = []
    with config_guard(bench, 1) as before:
        if token(before[1], "?CMDCHAR,") != "?CMDCHAR,;":
            raise Skip("W1's command character is not ';'")
        try:
            for line, want in checks:
                m = w.dev.mark()
                w.dev.send(line)
                time.sleep(0.6)
                out = [x.rstrip() for x in w.dev.since(m)]
                if want not in out:
                    problems.append(f"{line!r} printed {out}")
                    chars = _prefix_chars(w)
                    if chars != ("^", "?", ";"):
                        problems.append(f"{line!r} changed the characters to {chars!r}")
                        _restore_prefixes(w)
        finally:
            if _prefix_chars(w) != ("^", "?", ";"):
                _restore_prefixes(w)
        cfg = _cfg(w)
    missing = [x for x in ("Delimiter Character:      ^", "Local Function Identifier: ?", "Command Character:         ;")
               if x not in cfg]
    assert not problems, "; ".join(problems)
    assert not missing, f"?config lacks {missing}"


@test("wcb.erase_bad_argument", "?ERASE, ?ERASE,NV and ?ERASE,NVSX print the usage line and erase nothing: no restart follows within 6 s, no NVS namespace loses an entry, and the chain is unchanged", needs=["wcb1"], links=[])
def erase_bad_argument(bench):
    """WCB-WP35 row 3. processLocalCommand's ERASE branch (WCB.ino) calls eraseNVSFlash only when the argument, upper-
    cased, is NVS - so '?ERASE,nvs' erases too and is never sent - and prints 'Invalid format. Use: ?ERASE,NVS' for
    anything else. A regression here wipes W1, so the test runs only when W1's chain can be replayed a line at a time,
    the erase tests' restore path (s31 _replayable); the pre-test ?backup is in session.log."""
    from suites.s31_password_erase import _nvs, _replayable       # at run time: a module-level import reorders the registry
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        _replayable(before[1])
        stats0, spaces0 = _nvs(w.run("?NVS", timeout=6))
        if not stats0:
            raise Skip("?NVS printed no usage line")
        m = w.dev.mark()
        outs = {cmd: [x.rstrip() for x in w.run(cmd)] for cmd in ("?ERASE", "?ERASE,NV", "?ERASE,NVSX")}
        time.sleep(6)
        after = [x.rstrip() for x in w.dev.since(m)]
        _, spaces1 = _nvs(w.run("?NVS", timeout=6))
    bad = [f"{cmd} printed {out}" for cmd, out in outs.items() if "Invalid format. Use: ?ERASE,NVS" not in out]
    if any(x.startswith("Rebooting now") or "Booting up the Wireless Communication Board" in x for x in after):
        bad.append("W1 restarted")
    lost = {k: (v, spaces1.get(k)) for k, v in spaces0.items() if spaces1.get(k, 0) < v}
    if lost:
        bad.append(f"NVS namespaces lost entries (before, after): {lost}")
    assert not bad, "; ".join(bad)


@test("chars.legacy_lf_sets", "Legacy ?LF! sets the function identifier ('LocalFunctionIdentifier updated to '!''), !VERSION and !CONFIG answer under it, and !FUNCCHAR,? puts it back", needs=["wcb1"], links=[])
def legacy_lf_sets(bench):
    """WCB-WP35 row 4. updateLocalFunctionIdentifier (WCB.ino) is reached from processLocalCommand's legacy branch for
    ?LF<c> only, separately from ?FUNCCHAR; only its refusals were tested (chars.legacy_lf_guard, chars.format_refusals).
    While the identifier is '!' every '?' line is a plain broadcast, so the window uses dev.send and sends nothing
    starting with '?' until '!FUNCCHAR,?' (FUNCCHAR is exempt from the trailing-'?' help shortcut)."""
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        if token(before[1], "?CMDCHAR,") != "?CMDCHAR,;":
            raise Skip("W1's command character is not ';'")
        try:
            _sent(w, "?LF!", r"^LocalFunctionIdentifier updated to '!'")
            _sent(w, "!VERSION", r"^End of Version")
            _sent(w, "!CONFIG", r"Local Function Identifier: !", timeout=5)
            _sent(w, "!FUNCCHAR,?", r"^Local function identifier updated to '\?'")
            _sent(w, "?VERSION", r"^End of Version")
        finally:
            _, lfi = _live_chars(w)
            if lfi != "?":
                w.dev.send(f"{lfi}FUNCCHAR,?")
                time.sleep(0.5)


@test("etm.query_range_forms", "Bare ?ETM,TIMEOUT / BOOT / DELAY are queries matching the chain; out-of-range values print their exact refusals; the legacy ?ETMTIMEOUT / ?ETMBOOT / ?ETMCHARDELAY / ?ETMMISS have the same query and range forms ('(currently N)'); nothing changes", needs=["wcb1"], links=[])
def etm_query_range_forms(bench):
    """WCB-WP48 row 8. processLocalCommand's ETM branch treats an empty value as a query for TIMEOUT, BOOT and DELAY
    (a bare setter used to write 0 to NVS: etm.unvalidated_setters) and range-checks TIMEOUT 50-10000, BOOT 1-30 and
    DELAY 0-5000; the legacy ?ETMTIMEOUT / ?ETMBOOT / ?ETMCHARDELAY / ?ETMMISS branches do the same in their own words
    (all WCB.ino). Legacy ?ETMHB has no query form (a bare one is refused), so it is not in the list."""
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        o = _etm(before[1])
        checks = [("?ETM,TIMEOUT", f"ETM timeout is {o['TIMEOUT']} ms"), ("?ETM,BOOT", f"ETM boot window is {o['BOOT']} sec"),
                  ("?ETM,DELAY", f"ETM char delay is {o['DELAY']} ms"),
                  ("?ETM,TIMEOUT,49", "Invalid ETM timeout '49'. Use 50-10000 ms."),
                  ("?ETM,TIMEOUT,10001", "Invalid ETM timeout '10001'. Use 50-10000 ms."),
                  ("?ETM,BOOT,0", "Invalid ETM boot window '0'. Use 1-30 sec."),
                  ("?ETM,BOOT,31", "Invalid ETM boot window '31'. Use 1-30 sec."),
                  ("?ETM,DELAY,-1", "Invalid ETM char delay '-1'. Use 0-5000 ms."),
                  ("?ETM,DELAY,5001", "Invalid ETM char delay '5001'. Use 0-5000 ms."),
                  ("?ETMTIMEOUT", f"ETM timeout is {o['TIMEOUT']} ms"), ("?ETMBOOT", f"ETM boot window is {o['BOOT']} sec"),
                  ("?ETMCHARDELAY", f"ETM char delay is {o['DELAY']} ms"),
                  ("?ETMMISS", f"ETM missed heartbeats is {o['MISS']}"),
                  ("?ETMTIMEOUT49", f"Invalid ETM timeout. Use 50-10000 ms (currently {o['TIMEOUT']})."),
                  ("?ETMBOOT40", f"Invalid ETM boot window. Use 1-30 seconds (currently {o['BOOT']})."),
                  ("?ETMCHARDELAY6000", f"Invalid ETM char delay. Use 0-5000 ms (currently {o['DELAY']})."),
                  ("?ETMMISS101", f"Invalid ETM missed-heartbeat count. Use 1-100 (currently {o['MISS']}).")]
        try:
            bad = [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
        finally:
            now = _etm(snapshot(bench, 1))
            changed = tuple(k for k in ETM_KEYS if now[k] != o[k])
            if changed:                         # nothing should have changed: put a regression back before the guard
                _restore_etm(w, o, changed)
    assert not bad, "; ".join(bad)


@test("stats.rpt_large_ram_only", "?STATS,RPT stores a counter above 2^31 exactly (Sent: 3000000000), and reported rows live in RAM only: a W1 reboot clears them (1 reboot)", needs=["wcb1"], links=[])
def rpt_large_ram_only(bench):
    """WCB-WP48 row 9. storeReportedStats (WCB.ino) parses with strtoul into an unsigned long - toInt() saturated at 2^31
    - and reportedStats[] is never persisted: a reboot clears it by design (the comment at its declaration). Reporter 7
    is no bench board, so nothing can report as it again after the reboot."""
    w = usb_wcb(bench)
    if [x for x in w.run("?STATS") if x.startswith("WCB7: ")]:
        raise Skip("WCB7 already reports to W1")
    rebooted = False
    try:
        out = [x.rstrip() for x in w.run("?STATS,RPT,7,3000000000,1,1,1,1,1,1") if x.startswith("[STATS]")]
        stats = _stats(w)
        w.reboot()
        rebooted = True
        after = _stats(w)
    finally:
        if not rebooted and any(x.startswith("WCB7: ") for x in w.run("?STATS")):
            w.run("?STATS,RESET")
        _peers_back(w)
    assert not out, f"a valid RPT printed {out}"
    assert any(re.match(r"^WCB7: Sent: 3000000000, ACKd: 1, Retries: 1, Failed: 1, Unguaranteed: 1, Bcast: 1, Recv: 1  \(\d+s ago\)$", x)
               for x in stats), f"no exact WCB7 row: {[x for x in stats if x.startswith('WCB7: ')]}"
    assert not [x for x in after if x.startswith("WCB7: ")], "the reported WCB7 row survived the reboot"


@test("alias.utf8_truncation", "?ALIAS cuts at 24 bytes on a UTF-8 boundary: 23 ASCII characters + 'é' keeps just the 23, 22 + 'é' keeps all 24 bytes; the alias is put back", needs=["wcb1"], links=[])
def alias_utf8_truncation(bench):
    """WCB-WP48 row 7. saveWCBAlias (WCB_Storage.cpp) caps the alias at 24 bytes and backs the cut up over UTF-8
    continuation bytes (10xxxxxx), so NVS never holds half a character. 'é' is C3 A9; SerialDevice decodes the console
    as UTF-8 with replacement, so a stray lead byte would show as U+FFFD and fail the comparison."""
    w = usb_wcb(bench)
    base = "HIL" + nonce() * 4
    a23, a22 = base[:23], base[:22]
    bad = []
    with config_guard(bench, 1) as before:
        orig = token(before[1], "?ALIAS,")
        try:
            for text, want in ((a23 + "é", a23), (a22 + "é", a22 + "é")):
                out = [x.rstrip() for x in w.run(f"?ALIAS,{text}")]
                if f"WCB alias set to: {want}" not in out:
                    bad.append(f"?ALIAS,{text} printed {out}")
                query = [x.rstrip() for x in w.run("?ALIAS")]
                if f"Alias: {want}" not in query:
                    bad.append(f"?ALIAS after {text!r}: {query}")
        finally:
            w.run(orig if orig else "?ALIAS,CLEAR")
            time.sleep(2)           # the on-change advert (checked every 500 ms) carries the alias back out
    assert not bad, "; ".join(bad)


def _local_devices(tokens, n, verb):
    """[(id, baud)] of W<n>'s own ?MAESTRO (verb 'MAESTRO,M') or ?WLED (verb 'WLED,') slots in chain order, which is
    slot order: emitMaestroBackup / emitWLEDBackup and wdpLocalMaestroCfg / wdpLocalWLEDCfg all walk the slots in turn.
    A remote proxy names another board (or S0) and is left out, as the advert leaves it out."""
    rx = re.compile(rf"^\?{verb}(\d+):W{n}S([1-5]):(\d+)$", re.I)
    return [(int(m.group(1)), int(m.group(3))) for t in tokens for m in [rx.match(t)] if m]


def _id_baud(pairs):
    """?WDP,DUMP's '<id>@<baud>' list, dot-joined ('-' when empty; wdpIdBaudStr)."""
    return ".".join(f"{i}@{b}" if b in _WDP_BAUDS else f"{i}" for i, b in pairs) or "-"


def _detail_list(pairs):
    """printWdpDetail's Maestros / WLED value: ' <id>@<baud>' per device, ' -' when none."""
    return "".join(f" {i}@{b}" if b in _WDP_BAUDS else f" {i}" for i, b in pairs) or " -"


@test("wdp.dump_neighbor_fields", "?WDP,DUMP's HW=, MAESTRO= and [WDPX:MB=,WL=] for the self row and for W2's row match each board's own ?HW / ?MAESTRO / ?WLED config (id@baud in slot order; no WDPX line for a board that hosts neither)", needs=["wcb1"], links=[])
def dump_neighbor_fields(bench):
    """WCB-WP48 row 3. printWdpDump (WCB_WDP.cpp) builds the self row from wcb_hw_version, wdpLocalMaestroCfg and
    wdpLocalWLEDCfg, and W2's row from its advert's HWVER, MAESTRO_CFG and WLED_CFG TLVs; wdpEmitDumpX writes
    [WDPX:N=,MB=,WL=] only for a board that hosts a Maestro or a WLED, and wdpIdBaudStr prints a bare id for a baud with
    no WDP code. The neighbour-side [WDPPWM:N=1,DST=2,S=3] edge of the plan's row belongs in pwm.passthrough_mesh
    (s14_pwm.py) and is not checked here."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    dump = [x for x in _dump(w) if x.startswith("[WDP")]
    bad = []
    for n in (me, 2):
        tokens = snapshot(bench, n)
        row = _row(dump, n)
        if row is None:
            bad.append(f"no DUMP row for W{n}")
            continue
        hw = (token(tokens, "?HW,") or "?HW,?").split(",")[1]
        if _field(row, "HW") != hw:
            bad.append(f"W{n} HW={_field(row, 'HW')}, its chain says ?HW,{hw}")
        maestros, wleds = _local_devices(tokens, n, "MAESTRO,M"), _local_devices(tokens, n, "WLED,")
        ids = ".".join(str(i) for i, _ in maestros) or "-"
        if _field(row, "MAESTRO") != ids:
            bad.append(f"W{n} MAESTRO={_field(row, 'MAESTRO')}, expected {ids}")
        rows_x = [x for x in dump if x.startswith(f"[WDPX:N={n},")]
        want = [f"[WDPX:N={n},MB={_id_baud(maestros)},WL={_id_baud(wleds)}]"] if maestros or wleds else []
        if rows_x != want:
            bad.append(f"W{n} WDPX {rows_x}, expected {want}")
    assert not bad, "; ".join(bad)


@test("wdp.list_detail_content", "W2's ?WDP,LIST row (Platform, Cap codes, Maestros, live) and its ?WDP,2 detail (Platform, Capabilities with the controller id, Maestros and WLED id@baud) match W2's own config, and '(not heard)' marks exactly the WDP-DA devices W1's DUMP has as SEEN=0", needs=["wcb1"], links=[])
def list_detail_content(bench):
    """WCB-WP48 row 4. printWdpList and printWdpDetail (WCB_WDP.cpp): the Cap column is wdpCapCodes (M R K H 3 W P C D,
    space-separated) cut to 12 characters by its %-12.12s column, Maestros is wdpMaestroStr (dot-joined ids) cut to 10;
    the detail spells the same bits out with wdpCapNames and adds '(controller ID n)' when the Controller bit is set
    and the advert carries an id. The legend's D=DFPlayer (re-scan #30, fixed) is pinned by wdp.list_detail_errors."""
    w = usb_wcb(bench)
    t2 = snapshot(bench, 2)
    dump = [x.rstrip() for x in _dump(w)]
    row2 = _row(dump, 2)
    if row2 is None:
        raise Skip("W1 has no WDP row for W2")
    alias = (token(t2, "?ALIAS,") or "?ALIAS,")[len("?ALIAS,"):]
    if not alias.isascii():
        raise Skip("W2's alias is not ASCII: the LIST columns are sliced by character")
    cap = _expected_cap(t2, 2)
    codes = " ".join(c for bit, c, _ in _CAPS if cap & bit) or "-"
    names = ", ".join(name for bit, _, name in _CAPS if cap & bit) or "none"
    maestros, wleds = _local_devices(t2, 2, "MAESTRO,M"), _local_devices(t2, 2, "WLED,")
    ids = ".".join(str(i) for i, _ in maestros) or "-"
    hw = int((token(t2, "?HW,") or "?HW,0").split(",")[1] or 0)
    platform = "ESP32-S3" if hw >= 31 else "ESP32" if hw > 0 else "?"
    bad = []
    if _field(row2, "CAP") != "%04X" % cap:
        bad.append(f"W1's DUMP has W2 CAP={_field(row2, 'CAP')}, W2's config gives {cap:04X}")
    line = next((x for x in (y.rstrip() for y in w.run("?WDP,LIST")) if x.startswith("2   ")), None)
    if line is None:
        bad.append("no ?WDP,LIST row for WCB2")
    else:
        # "%-4d  %-16.16s  %-10.10s  %-12.12s  %-10.10s  %-5s  %-5s": WCB, Alias, Platform, Cap, Maestros, Age, State
        got = {"platform": line[24:34].strip(), "cap": line[36:48].strip(), "maestros": line[50:60].strip(),
               "state": line[69:].strip()}
        want = {"platform": platform[:10], "cap": codes[:12].strip(), "maestros": ids[:10], "state": "live"}
        bad += [f"LIST {k} {got[k]!r}, expected {v!r}" for k, v in want.items() if got[k] != v]
    detail = [x.rstrip() for x in w.run("?WDP,2")]
    ctrl = _field(row2, "CTRL")
    cap_line = f"  Capabilities: {names}" + (f"  (controller ID {ctrl})" if cap & 0x0040 and ctrl not in (None, "0") else "")
    for want_line in (f"  Platform    : {platform} (hw {hw})", cap_line, "  Maestros    :" + _detail_list(maestros),
                      "  WLED        :" + _detail_list(wleds), "  Interfaces  :"):
        if want_line not in detail:
            bad.append(f"?WDP,2 lacks {want_line!r}")
    devices = [m for x in dump for m in [re.match(r"^\[WDPDA:N=2,S=(\d),TYPE=([^,]*),.*,SEEN=([01]),AGE=-\]$", x)] if m]
    dev_lines = [x for x in detail if x.startswith("          ") and " fw " in x]
    for m in devices:
        kind, seen = m.group(2)[:24], m.group(3) == "1"
        hit = next((x for x in dev_lines if x.strip().startswith(kind)), None)
        if hit is None:
            bad.append(f"?WDP,2 does not list the WDP-DA device {kind!r} on S{m.group(1)}")
        elif ("(not heard)" in hit) == seen:
            bad.append(f"?WDP,2 device line {hit.strip()!r} vs SEEN={m.group(3)} in W1's DUMP")
    if not devices and any("(not heard)" in x for x in detail):
        bad.append("'(not heard)' with no WDP-DA device of W2's in W1's DUMP")
    bench.note(f"W2: CAP {cap:04X} ({codes}), Maestros {ids}, {len(devices)} WDP-DA device(s)")
    assert not bad, "; ".join(bad)


def _poll_fresh(w, n, tries=3):
    """?WDP,POLL until W<n>'s row is at most 3 s old -> True when it was. The solicit and the answer are single
    unacknowledged broadcasts, so one can be lost (wdp.poll_refreshes_age)."""
    for _ in range(tries):
        w.run("?WDP,POLL")
        time.sleep(3)
        if int(_field(_row(_dump(w), n), "AGE") or 99) <= 3:
            return True
    return False


def _poll_learned(w, n, since, tries=3):
    """?WDP,POLL until '[WDP] learned WCB<n>' shows after mark `since` -> True when it did (same loss as _poll_fresh)."""
    for _ in range(tries):
        w.run("?WDP,POLL")
        try:
            w.dev.expect(rf"\[WDP\] learned WCB{n}\b", timeout=3, since=since)
            return True
        except AssertionError:
            continue
    return False


@test("wdp.controller_no_readopt_no_override", "Controller auto-adopt fires only on the first learn of a controller-type device, and only with no controller enabled: re-hearing NaviCore while the controller is OFF does not re-enable it, relearning NaviCore does not move a controller pinned at 17, and a 'Sabe' device at 14 is adopted once the controller is OFF (auto-join off; ~45 s)", needs=["wcb1", "probe2"], links=[])
def controller_no_readopt_no_override(bench):
    """WCB-WP48 row 5. wdpOnAdvertReceived (WCB_WDP.cpp) calls enableControllerPeer only for a neighbour's first advert
    (!wasValid), a controller device type (wdpIsControllerType: NaviCore, Sabé or Sabe, any case) and no controller
    enabled. Auto-join is off throughout: with the controller off or moved, NaviCore (non-temporary) would otherwise be
    learned as a persisted peer. The probe joins as a temporary client of type 'Sabe' (MESH JOIN TYPE=), sends nothing
    and leaves; ?CONTROLLER,ON,20 goes back in the finally."""
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1] or _row(_dump(w), 20) is None:
            raise Skip("controller 20 is not enabled, or NaviCore is not in W1's WDP table")
        _require_id_free(w, 14)
        _require_id_free(w, 17)
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            # (a) NaviCore is heard again while the controller is OFF: its row is still valid, so no adopt
            if not _has(w.run("?CONTROLLER,OFF"), "Controller peer (ID 20) DISABLED."):
                bad.append("(a) ?CONTROLLER,OFF did not confirm")
            am = w.dev.mark()
            if not _poll_fresh(w, 20):
                bench.note("(a) NaviCore's row was not refreshed by three polls: nothing was heard to re-adopt")
            if _has(w.dev.since(am), "auto-enabling controller peer"):
                bad.append("(a) re-hearing NaviCore re-enabled the controller")
            if not _has(w.run("?CONTROLLER"), "Controller peer (ID 20) is currently DISABLED."):
                bad.append("(a) the controller did not stay DISABLED")
            # (b) NaviCore is relearned (FORGET, then POLL) while the controller is pinned at 17
            on17 = [x.rstrip() for x in w.run("?CONTROLLER,ON,17")]
            if "Controller peer (ID 17) ENABLED." not in on17:
                bad.append(f"(b) ?CONTROLLER,ON,17 printed {on17}")
            bm = w.dev.mark()
            w.run("?WDP,FORGET,20")
            if not _poll_learned(w, 20, bm):
                raise Skip("NaviCore did not answer three polls after ?WDP,FORGET,20, so (b) and (c) cannot run")
            time.sleep(0.5)
            if _has(w.dev.since(bm), "auto-enabling controller peer"):
                bad.append("(b) relearning NaviCore moved the pinned controller")
            if not _has(w.run("?CONTROLLER"), "Controller peer (ID 17) is currently ENABLED."):
                bad.append("(b) the controller did not stay at 17")
            # (c) a 'Sabe' device appears while the controller is OFF: adopted at its id
            if not _has(w.run("?CONTROLLER,OFF"), "Controller peer (ID 17) DISABLED."):
                bad.append("(c) ?CONTROLLER,OFF did not confirm")
            cm = w.dev.mark()
            with _joined(bench, "probe2", 14, dev_type="Sabe"):
                try:
                    w.dev.expect(r'\[WDP\] heard controller "Sabe" \(WCB14\) — auto-enabling controller peer', timeout=15, since=cm)
                except AssertionError:
                    bad.append("(c) the 'Sabe' device was not adopted as the controller")
                time.sleep(0.5)
                adopted = [x.rstrip() for x in w.dev.since(cm)]
                query = w.run("?CONTROLLER")
            for want in ("[WDP] learned WCB14 Sabe", "Controller peer ID set to 14.", "Controller peer (ID 14) ENABLED."):
                if not _has(adopted, want):
                    bad.append(f"(c) no {want!r}")
            if not _has(query, "Controller peer (ID 14) is currently ENABLED."):
                bad.append(f"(c) ?CONTROLLER printed {query}")
        finally:
            if not _has(w.run("?CONTROLLER"), "Controller peer (ID 20) is currently ENABLED."):
                w.run("?CONTROLLER,ON,20")
            w.run("?WDP,AUTOJOIN,ON")
            if _row(_dump(w), 20) is None:
                w.run("?WDP,POLL")
    assert not bad, "; ".join(bad)


@test("peers.controller_other_id_persist", "?CONTROLLER,ON,17 moves the controller live and persists it: 'registered (live)', ?CONTROLLER says 17, the chain holds ?CONTROLLER,ON,17, ;W20 is refused and ?STATS has a 'WCB17 (special)' row - all still so after a reboot; ON,20 makes NaviCore answer again with ?PEERSLIVE unchanged (auto-join off; 1 reboot)", needs=["wcb1"], links=[])
def controller_other_id_persist(bench):
    """WCB-WP48 row 6. enableControllerPeer (WCB.ino) deletes the old out-of-band controller peer, saves the new id
    (saveSpecialPeerIDToPreferences, WCB_Storage.cpp: 'Controller peer ID set to n.') and registers it live; setup()
    registers the saved id at boot. Auto-join is off, so NaviCore - a plain client while 17 is the controller - is not
    learned as a persisted peer. The other half of the row (a disabled controller's commands still run) is
    peers.controller_off_still_executes."""
    w = usb_wcb(bench)
    bad = []
    refused = "WCB 20 is not a reachable target — it isn't a configured or learned peer or the controller."

    def check(when):
        if not _has(w.run("?CONTROLLER"), "Controller peer (ID 17) is currently ENABLED."):
            bad.append(f"{when}: ?CONTROLLER does not say 17 ENABLED")
        if "?CONTROLLER,ON,17" not in snapshot(bench, 1):
            bad.append(f"{when}: the chain lacks ?CONTROLLER,ON,17")
        if not _has(w.run(";W20,x"), refused):
            bad.append(f"{when}: ;W20 is still a target")
        if _etm_row(_stats(w), 17, special=True) is None:
            bad.append(f"{when}: ?STATS has no 'WCB17 (special)' row")

    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1]:
            raise Skip("controller 20 is not enabled on W1")
        _require_id_free(w, 17)
        n0, _ = _live_peers(w)
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            out = [x.rstrip() for x in w.run("?CONTROLLER,ON,17")]
            for want in ("Controller peer ID set to 17.", "Controller peer WCB17 registered (live)."):
                if want not in out:
                    bad.append(f"?CONTROLLER,ON,17 lacks {want!r}")
            check("live")
            w.reboot()
            check("after a reboot")
        finally:
            back = [x.rstrip() for x in w.run("?CONTROLLER,ON,20")]
            w.run("?WDP,AUTOJOIN,ON")
        if "Controller peer ID set to 20." not in back:
            bad.append(f"?CONTROLLER,ON,20 printed {back}")
        wm = w.dev.mark()
        w.send(";W20,?version")
        try:
            w.dev.expect(r"^\[TERM:20\]End of Version", timeout=6, since=wm)
        except AssertionError:
            bad.append("NaviCore did not answer ;W20,?version after ON,20")
        if _live_peers(w)[0] != n0:
            bad.append(f"?PEERSLIVE went {n0} -> {_live_peers(w)[0]}")
        _peers_back(w)
    assert not bad, "; ".join(bad)


@test("peers.controller_off_still_executes", "With W1's controller OFF, a command NaviCore sends W1 (TEST_ACTION wcb_unicast ;S2<m>) is still ACKed and run: it arrives on W1 S2 once (auto-join off; under 20 s)", needs=["wcb1", "navicore"])
def controller_off_still_executes(bench):
    """WCB-WP48 row 6, second half. A disabled controller is not ignored on receive (docs/HIL_TESTING.md §6): the ETM
    receive path of espNowReceiveCallback ACKs (etmSendAck, which re-adds the sender as an ESP-NOW peer on demand) and
    runs a command from any valid in-group sender; only W1's own routing to it (;W20) is gated (WCB.ino). The ACK is
    read from ?DEBUG,ETM: 'Sent ACK seq N to WCB20' for the seq W1 printed as received."""
    s12 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    nc = NaviCore(bench.dev("navicore"))
    t = marker()
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1]:
            raise Skip("controller 20 is not enabled on W1")
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            w.run("?DEBUG,ETM,ON")
            off = w.run("?CONTROLLER,OFF")
            pm, wm = s12.mark(), w.dev.mark()
            ack = nc.ack_line({"type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": "1", "cmd": f";S2{t}"}})
            try:
                s12.expect(t.encode() + b"\r", timeout=3, since=pm)
            except AssertionError:
                pass
            time.sleep(1.0)
            got, lines = s12.received(pm), [x.rstrip() for x in w.dev.since(wm)]
        finally:
            if not _has(w.run("?CONTROLLER"), "Controller peer (ID 20) is currently ENABLED."):
                w.run("?CONTROLLER,ON,20")
            w.run("?DEBUG,ETM,OFF")
            w.run("?WDP,AUTOJOIN,ON")
    assert _has(off, "Controller peer (ID 20) DISABLED."), f"?CONTROLLER,OFF printed {off}"
    assert ack == '{"type":"ACK","of":"TEST_ACTION","ok":true}', ack
    runs = got.count(t.encode() + b"\r")
    assert runs == 1, f"W1 S2 got the command {runs} times: {got!r}"
    seq = next((m.group(1) for x in lines for m in [re.search(rf"\[ETM\] Received seq (\d+) from WCB20: ;S2{t}$", x)] if m), None)
    assert seq, "W1 printed no 'Received seq N from WCB20' for the command"
    assert any(x.startswith(f"[ETM] Sent ACK seq {seq} to WCB20") for x in lines), f"W1 did not ACK seq {seq} to WCB20"


def _seq_of(lines, text):
    """The seq of W1's '[ETM] Sent seq N: <text>' debug line, or None."""
    return next((m.group(1) for x in lines for m in [re.search(rf"\[ETM\] Sent seq (\d+): {re.escape(text)}$", x)] if m),
                None)


@test("etm.controller_unicast_tracked", "A ;W20 unicast to the controller (never a wcbPeerActive member) expects NaviCore's ACK and counts in the 'WCB20 (special)' ?STATS row; a plain broadcast does not expect the controller, so the row stays at Sent 1, ACKd 1", needs=["wcb1"], links=[])
def controller_unicast_tracked(bench):
    """WCB-WP30 row 1, tracked half. etmAddToPendingTable (WCB.ino) adds the controller's slot to expectAckFrom only for
    a unicast addressed to it (isSpecialPeerSlot), never for a broadcast, on purpose; etmProcessAck counts only an ACK
    it expected, so NaviCore's ACK of the broadcast (a WCB_Client ACKs every command) leaves the row alone (tracker
    #22). The retry half is etm.controller_unicast_retry."""
    w = usb_wcb(bench)
    if "?CONTROLLER,ON,20" not in snapshot(bench, 1):
        raise Skip("controller 20 is not enabled on W1")
    row = _etm_row(_stats(w), 20, special=True)
    if not row or not row[4]:
        w.run("?WDP,POLL")
        time.sleep(3)
        row = _etm_row(_stats(w), 20, special=True)
        if not row or not row[4]:
            raise Skip("W1's ?STATS does not show the controller WCB20 online")
    t = f"hilbc{nonce().lower()}"            # a plain broadcast reaches every broadcast port on both WCBs: inert text
    try:
        w.run("?DEBUG,ETM,ON")
        w.run("?STATS,RESET")
        wm = w.dev.mark()
        w.send(";W20,?version")
        w.dev.expect(r"^\[TERM:20\]End of Version", timeout=5, since=wm)
        time.sleep(0.5)
        lines = [x.rstrip() for x in w.dev.since(wm)]
        row1 = _etm_row(_stats(w), 20, special=True)
        bm = w.dev.mark()
        w.send(t)
        time.sleep(1.5)
        blines = [x.rstrip() for x in w.dev.since(bm)]
        row2 = _etm_row(_stats(w), 20, special=True)
    finally:
        w.run("?DEBUG,ETM,OFF")
    seq, bseq = _seq_of(lines, "?version"), _seq_of(blines, t)
    assert seq, "no 'Sent seq' line for ;W20,?version"
    assert f"[ETM] ACK received from WCB20 for seq {seq}" in lines and f"[ETM] Seq {seq} fully acknowledged" in lines, \
        "the unicast to the controller was not tracked to its ACK"
    assert row1 and row1[:2] == (1, 1) and row1[3] == 0, f"WCB20 (special) row after the unicast: {row1}"
    assert bseq, "no 'Sent seq' line for the broadcast"
    bench.note(f"NaviCore's ACK of the broadcast seen: {f'[ETM] ACK received from WCB20 for seq {bseq}' in blines}")
    assert row2 and row2[:2] == (1, 1) and row2[3] == 0, f"the broadcast changed the WCB20 (special) row: {row1} -> {row2}"


@test("mgmt.frag_chksm_boundary", "Under ?ETM,CHKSM a single-chunk ?MGMT,FRAG payload of 180 or 186 characters still goes to W2 as one SOH-marked ETM unicast and runs; 187 is refused as over 179 and nothing arrives", needs=["wcb1"])
def frag_chksm_boundary(bench):
    """WCB-WP30 row 9. handleMgmtForward (WCB.ino) sends a single-chunk FRAG as one ETM unicast behind a SOH marker while
    the payload fits singleChunkMax - under ?ETM,CHKSM that is ETM_MAX_CMD_WITH_CRC - 1 = 186, since the marker and the
    12-character '|CRC' suffix share the 199-character field - and otherwise falls to the fragment path, whose
    MGMT_PAYLOAD_SIZE - 1 = 179 ceiling refuses it (ungated). The CHKSM-off variant stays manual: s18 never changes
    W2's checksum setting."""
    s22 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?ETM,CHKSM,ON")
    bad = []
    try:
        w.run("?DEBUG,MGMT,ON")
        for n in (180, 186):
            text = padded(f"F{n}", n - 3)              # ';S2' + text = n characters
            sid = nonce()[:4]
            pm, wm = s22.mark(), w.dev.mark()
            w.send(f"?MGMT,FRAG,2,{sid},0,1,;S2{text}")
            try:
                s22.expect(text.encode() + b"\r", timeout=3, since=pm)
            except AssertionError:
                bad.append(f"the {n}-character payload did not run on W2")
            # Waited for, not read at once: the text reaches W2 S2 over the radio before W1 has printed its two
            # ~260-character lines (the echo, then this one) on its 115200 USB (run 20260928-064402).
            try:
                w.dev.expect(re.escape(f"[MGMT] Single-chunk cmd → ETM unicast to WCB2 session {sid}: ;S2{text}"),
                             timeout=3, since=wm)
            except AssertionError:
                bad.append(f"the {n}-character payload did not go as one ETM unicast")
        text = padded("F187", 184)
        pm, wm = s22.mark(), w.dev.mark()
        w.send(f"?MGMT,FRAG,2,{nonce()[:4]},0,1,;S2{text}")
        time.sleep(3)
        lines, got = [x.rstrip() for x in w.dev.since(wm)], s22.received(pm)
    finally:
        w.run("?DEBUG,MGMT,OFF")
    if "[MGMT] FRAG payload 187 > 179 chars — REJECTED (sender must fragment at 179)" not in lines:
        bad.append("no REJECTED line for the 187-character payload")
    if text.encode() in got:
        bad.append("the 187-character payload arrived on W2 S2")
    assert not bad, "; ".join(bad)


@test("etm.rx_debug_lines", "W2 under ?DEBUG,ON prints 'Processing ETM input from WCB1: ...' for an ETM command from W1, but an inbound ?STATS,RPT only under ?DEBUG,MGMT, beside its '[STATS] RPT from' line", needs=["wcb1"], links=[])
def rx_debug_lines(bench):
    """WCB-WP30 row 8, ETM half (the non-ETM lines are in etm.nonetm_whitelist_scope). The ETM receive path of
    espNowReceiveCallback (WCB.ino) mirrors a received command as 'Processing ETM input from WCB<n>: <cmd>' under
    ?DEBUG,ON, except a ?STATS,RPT - high-rate telemetry - which rides ?DEBUG,MGMT like storeReportedStats' own
    '[STATS] RPT from' line. The row W2 stores is cleared with ;W2,?STATS,RESET, as stats.rpt_remote does."""
    w = usb_wcb(bench)
    vals = [int(nonce(), 16) % 90000 + 10000 for _ in range(2)]
    rpt = [f"?STATS,RPT,9,{v},1,2,3,4,5,6" for v in vals]
    with Console(bench, 2) as c2:
        try:
            _crun(c2, "?DEBUG,ON")
            m = c2.mark()
            w.send(";W2,?PEERSLIVE")
            time.sleep(1.5)
            plain = [x.rstrip() for x in c2.lines(m)]
            m = c2.mark()
            w.send(f";W2,{rpt[0]}")
            time.sleep(1.5)
            quiet = [x.rstrip() for x in c2.lines(m)]
            _crun(c2, "?DEBUG,MGMT,ON")
            m = c2.mark()
            w.send(f";W2,{rpt[1]}")
            time.sleep(1.5)
            mgmt = [x.rstrip() for x in c2.lines(m)]
        finally:
            _crun(c2, "?DEBUG,MGMT,OFF")
            _crun(c2, "?DEBUG,OFF")
            w.send(";W2,?STATS,RESET")
            time.sleep(0.5)
    assert "Processing ETM input from WCB1: ?PEERSLIVE" in plain, f"no 'Processing ETM input' line under ?DEBUG,ON: {plain}"
    assert not _has(quiet, "Processing ETM input from WCB1: ?STATS,RPT") and not _has(quiet, "[STATS] RPT from WCB9"), \
        f"?STATS,RPT printed under ?DEBUG,ON alone: {quiet}"
    assert f"Processing ETM input from WCB1: {rpt[1]}" in mgmt, f"no 'Processing ETM input' line for RPT under ?DEBUG,MGMT: {mgmt}"
    assert f"[STATS] RPT from WCB9: sent={vals[1]} ackd=1 retries=2 failed=3 unguaranteed=4 bcast=5 recv=6" in mgmt, \
        "no '[STATS] RPT from WCB9' line under ?DEBUG,MGMT"


@test("etm.nonetm_whitelist_scope", "While only W1's ETM is off (under 20 s) W2 lets through only <cmdChar>M frames: ;W2,;M2,getErrors reaches W2's Maestro and W2's ?DEBUG,ON prints its 'Sender ID' and 'Processing ESP-NOW input' lines, while ;W2,?MAESTRO,LIST, ;W2,?VERSION and a broadcast 'xM...' are dropped with the mismatch line (re-scan #21)", needs=["wcb1"])
def nonetm_whitelist_scope(bench):
    """WCB-WP30 row 4 and the non-ETM half of row 8. espNowReceiveCallback's ETM-mismatch gate (WCB.ino) lets a plain
    frame through to an ETM board only as raw, PWM, RC JSON or a Maestro payload, and since re-scan #21 (fixed) a Maestro
    payload must start with ';' or the command character: testing only the second byte let '?MAESTRO,LIST' and 'xM...'
    through to run. The passed frame takes the normal receive path, which prints 'Sender ID: WCB1, Target ID: WCB2' and
    'Processing ESP-NOW input: ...' under ?DEBUG,ON. W2's console must be its own USB: a relayed terminal rides ETM,
    which W1 is not running. ;M2,getErrors only reads Maestro 2's error flags, as the bench's port_stimulus does."""
    tap, s23 = link(bench, 2, "S1"), link(bench, 2, "S3")
    own2 = bench.usb_wcbs().get(2)
    if not own2:
        raise Skip("W2 has no USB connection of its own to read its console on")
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON")
    w, w2 = usb_wcb(bench), WCB(bench.dev(own2))
    x = f"xMhil{nonce().lower()}"            # inert text on every port it reaches
    with config_guard(bench, 1) as before:
        if "?ETM,ON" not in before[1]:
            raise Skip("ETM is already off on W1")
        try:
            w2.run("?DEBUG,ON")
            off = w.run("?ETM,OFF")
            m2, tm, sm = w2.dev.mark(), tap.mark(), s23.mark()
            for cmd in (";W2,;M2,getErrors", ";W2,?MAESTRO,LIST", ";W2,?VERSION", x):
                w.send(cmd)
                time.sleep(0.8)
            time.sleep(1.0)
            lines2 = [y.rstrip() for y in w2.dev.since(m2)]
            tapped, got3 = tap.received(tm), s23.received(sm)
        finally:
            try:
                on = w.run("?ETM,ON")         # never leave W1 with ETM off: WDP and every ACK depend on it (rule 4)
            finally:
                w2.run("?DEBUG,OFF")
            w.run("?WDP,POLL")
    bad = []
    if not _has(off, "ETM disabled") or not _has(on, "ETM enabled"):
        bad.append(f"?ETM,OFF / ON on W1 printed {off} / {on}")
    if bytes.fromhex("AA0221") not in tapped:
        bad.append("the ;M2 getErrors frame did not reach W2's Maestro (the whitelist control)")
    for want in ("Sender ID: WCB1, Target ID: WCB2", "Processing ESP-NOW input: ;M2,getErrors"):
        if want not in lines2:
            bad.append(f"W2 did not print {want!r}")
    if _has(lines2, "------- Maestro Settings"):
        bad.append("W2 ran ?MAESTRO,LIST from a non-ETM frame")
    if "End of Version" in lines2:
        bad.append("W2 ran ?VERSION from a non-ETM frame")
    if x.encode() in got3:
        bad.append("W2 printed the non-ETM broadcast 'xM...'")
    drops = sum("Dropped non-ETM packet — ETM is ON here but sender has ETM OFF" in y for y in lines2)
    if drops < 3:
        bad.append(f"W2 printed the mismatch line {drops} times, expected 3 or more")
    assert not bad, "; ".join(bad)


@test("etm.off_fleet_normal_path", "With ETM off on both WCBs (under 40 s) the 249-byte normal path still delivers a ;W2 unicast, a plain broadcast, a single-chunk ?MGMT,FRAG (SOH marker stripped), a FRAG timer chain with its ~500 ms gap and a ;M2 get to W2's Maestro, and W1's ?STATS shows the MAC-level counters; both boards are put back to ETM on", needs=["wcb1"])
def off_fleet_normal_path(bench):
    """WCB-WP30 row 3. ETM off on every board is a supported mode (CLAUDE.md rule 4). sendESPNowMessage sends the plain
    frame; espNowReceiveCallback's normal path applies the target filter, forces the NUL, strips the SOH marker
    handleMgmtForward prepends, hands a timer chain to enqueuePendingTimerChain and rewrites an inbound ;M get
    (maestroRewriteInboundGet); buildStatsString shows espnowCommandAttempts / Success / Failed (all WCB.ino). W2 is
    switched on its own USB, never over the mesh, so either board can be put back whatever the mesh does, W2 first. WDP
    and NaviCore's ETM frames are ignored while the window lasts, so it is kept short."""
    s22, s23, tap = link(bench, 2, "S2"), link(bench, 2, "S3"), link(bench, 2, "S1")
    own2 = bench.usb_wcbs().get(2)
    if not own2:
        raise Skip("W2 has no USB connection of its own: its ETM is never switched over the mesh")
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON")
    w, w2 = usb_wcb(bench), WCB(bench.dev(own2))
    a, b, c, d = (marker(k) for k in "abcd")
    t = f"hilbc{nonce().lower()}"
    bad = []
    with config_guard(bench, 1, 2) as before:
        if "?ETM,ON" not in before[1] or "?ETM,ON" not in before[2]:
            raise Skip("ETM is not on on both WCBs")
        t0 = time.monotonic()
        try:
            if not _has(w2.run("?ETM,OFF"), "ETM disabled") or not _has(w.run("?ETM,OFF"), "ETM disabled"):
                bad.append("?ETM,OFF did not confirm")
            w.run("?STATS,RESET")
            pm = s22.mark()
            w.send(f";W2,;S2{a}")
            try:
                s22.expect(a.encode() + b"\r", timeout=3, since=pm)
            except AssertionError:
                bad.append("the ;W2 unicast did not run")
            stats = _stats(w)
            sm = s23.mark()
            w.send(t)
            try:
                s23.expect(t.encode() + b"\r", timeout=3, since=sm)
            except AssertionError:
                bad.append("the plain broadcast did not reach W2 S3")
            pm = s22.mark()
            w.send(f"?MGMT,FRAG,2,{nonce()[:4]},0,1,;S2{b}")
            try:
                s22.expect(b.encode() + b"\r", timeout=3, since=pm)
            except AssertionError:
                bad.append("the single-chunk FRAG did not run (SOH marker left on?)")
            pm = s22.mark()
            w.send(f"?MGMT,FRAG,2,{nonce()[:4]},0,1,;S2{c}^;T500^;S2{d}")
            gap = None
            try:
                s22.expect(d.encode() + b"\r", timeout=4, since=pm)
                tc, td = s22.time_of(c.encode(), pm), s22.time_of(d.encode(), pm)
                gap = td - tc if tc is not None and td is not None else None
            except AssertionError:
                bad.append("the FRAG timer chain did not finish")
            tm = tap.mark()
            w.send(";W2,;M2,getErrors")
            time.sleep(1.5)
            tapped = tap.received(tm)
        finally:
            try:
                on2 = w2.run("?ETM,ON")          # W2 first, on its own USB (rule 4: never leave the fleet split)
            finally:
                on1 = w.run("?ETM,ON")
                window = round(time.monotonic() - t0, 1)
                w.run("?WDP,POLL")
    bench.note(f"ETM-off window {window} s; FRAG timer gap {gap} ms")
    if not _has(on2, "ETM enabled") or not _has(on1, "ETM enabled"):
        bad.append(f"?ETM,ON printed W2 {on2} / W1 {on1}")
    if "  Transmission Attempts: 1, Delivered: 1, Failed: 0" not in stats or "  Delivery Success Rate: 100.00%" not in stats:
        bad.append(f"?STATS with ETM off: {[x for x in stats if 'Transmission' in x or 'Success Rate' in x]}")
    if _has(stats, "--------------- ETM Per-Board Statistics ---------------"):
        bad.append("?STATS kept the ETM per-board block with ETM off")
    if gap is not None and not 400 <= gap <= 900:
        bad.append(f"the FRAG timer chain's gap was {gap} ms, expected ~500")
    if bytes.fromhex("AA0221") not in tapped:
        bad.append("the ;M2 get did not reach W2's Maestro")
    if window > 40:
        bad.append(f"the ETM-off window lasted {window} s")
    assert not bad, "; ".join(bad)


# ------------------------------------------------------------ the probe as a mesh client (slow; last)
def _forget_everywhere(bench, n, navicore=False):
    """?WDP,FORGET,<n> on every WCB and, after a PERMANENT join, FORGET_PEER to NaviCore over the mesh: a WCB_Client
    learns and persists a non-temporary device as a WCB does. Each persists its removal."""
    w = usb_wcb(bench)
    w.run(f"?WDP,FORGET,{n}")
    for k in remote_wcbs(bench):
        w.send(f";W{k},?WDP,FORGET,{n}")
        time.sleep(0.4)
    if navicore:
        w.send(f';W20,{{"type":"FORGET_PEER","id":{n}}}')
    time.sleep(1.5)


@contextmanager
def _joined(bench, probe_name, device_id, temporary=True, dev_type="HILProbe", forget=True):
    """probe_in_mesh (suites/common.py) with the two things it pins set free. temporary=False joins as a PERMANENT
    client, which every WCB learns as a persisted peer on its second advert (wdpOnAdvertReceived, WCB_WDP.cpp) and
    NaviCore too (WCB_Client auto-join); dev_type is the advertised device type (a controller type is adopted as the
    controller by a WCB that has none). No PEER=2 refusal: the downgrade test rejoins its learned id on purpose. The
    probe sends nothing unless a test makes it, so an id may be shared (the MESH_IDS rule, s19). forget=True forgets the
    id on every WCB after the leave and, for a permanent join, on NaviCore."""
    if device_id in FORBIDDEN_MESH_IDS:
        raise AssertionError(f"mesh id {device_id} is reserved on this bench")
    params = mesh_params(bench)
    probe = bench.probe(probe_name)
    for l in bench.links.all():
        if l.probe_name == probe_name:
            bench.links.release(l)
    probe.mesh_join(device_id, params["oct2"], params["oct3"], params["password"], params["quantity"],
                    channel=params["channel"], checksum=params["checksum"], temporary=temporary, dev_type=dev_type)
    try:
        yield probe
    finally:
        try:
            if probe.mesh_id:             # a test that timed its own leave has left already
                probe.mesh_leave()
        finally:
            bench.links.forget_probe(probe_name)
            if forget:
                _forget_everywhere(bench, device_id, navicore=not temporary)


def _await_line(w, pattern, since, tries=3, wait=8.0):
    """Wait for a W1 line after mark `since`, sending ?WDP,POLL between tries: the probe answers a solicit with an
    advert, so a lost boot-burst advert costs a poll instead of the next periodic advert (60 s for a permanent client).
    -> the re.Match; raises AssertionError after the last try."""
    for k in range(tries):
        try:
            return w.dev.expect(pattern, timeout=wait, since=since)
        except AssertionError:
            if k == tries - 1:
                raise
            w.run("?WDP,POLL")


def _probe_rx(probe, since, text, sender, timeout=3.0):
    """How many commands starting with `text` the probe received from WCB<sender>, after waiting up to `timeout` for one
    (a checksummed client strips the '|CRC' suffix; startswith covers one that does not)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not any(s == sender and x.startswith(text) for s, x in probe.mesh_received(since)):
        time.sleep(0.1)
    return sum(1 for s, x in probe.mesh_received(since) if s == sender and x.startswith(text))


def _restored(lines):
    """The count in setup()'s '[PEER] restored <n> learned peer(s) from NVS' (loadLearnedPeers, WCB.ino), 0 when there
    is no such line (it is printed only for n > 0)."""
    m = next((m for x in lines for m in [re.search(r"\[PEER\] restored (\d+) learned peer\(s\) from NVS", x)] if m), None)
    return int(m.group(1)) if m else 0


@test("etm.controller_unicast_retry", "With the probe standing in as W1's controller (?CONTROLLER,ON,15), a unicast to it is tracked to its ACK in the 'WCB15 (special)' row; once the probe has left (still online to W1) the next one is retried 3 times at the ?ETM,TIMEOUT spacing and counted failed (auto-join off; ~35 s)", needs=["wcb1", "probe2"], links=[])
def controller_unicast_retry(bench):
    """WCB-WP30 row 1, retry half. The plan loses NaviCore's ACK by deafening W1 with a live ?MAC,3 change (_deaf_w1);
    these tests never change ?MAC, so the controller id moves to a probe, which MESH LEAVE silences while W1 still counts
    it online. The special-peer path depends only on WCB_SPECIAL_PEER_ID: etmAddToPendingTable's isSpecialPeerSlot and
    the 3 retries of processETMAcksAndRetries (WCB.ino). The id is set BEFORE the probe joins, so W1 never adopts it
    as a temporary peer (addTemporaryPeer and auto-join both skip the controller); auto-join is off so NaviCore, a plain
    client meanwhile, is not learned as a persisted peer."""
    X = 15
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _require_id_free(w, X)
    t1, t2 = marker("u"), marker("r")
    bad = []
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1]:
            raise Skip("controller 20 is not enabled on W1")
        spacing = int(_etm(before[1])["TIMEOUT"])
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            w.run("?DEBUG,ETM,ON")
            on = [x.rstrip() for x in w.run(f"?CONTROLLER,ON,{X}")]
            w.run("?STATS,RESET")
            wm = w.dev.mark()
            with _joined(bench, "probe2", X) as probe:
                _await_line(w, rf"\[ETM\] WCB{X} came ONLINE", wm)
                pm, am = probe.dev.mark(), w.dev.mark()
                w.send(f";W{X},{t1}")
                runs = _probe_rx(probe, pm, t1, me)
                time.sleep(0.5)
                acked = [x.rstrip() for x in w.dev.since(am)]
                row1 = _etm_row(_stats(w), X, special=True)
                probe.mesh_leave()               # silent from here, but W1 counts it online for (HB+1) x MISS
                rm = w.dev.mark()
                w.send(f";W{X},{t2}")
                time.sleep(4 * spacing / 1000 + 1.5)
                entries = _timed(w.dev, rm)
                row2 = _etm_row(_stats(w), X, special=True)
            joined = [x.rstrip() for x in w.dev.since(wm)]
        finally:
            w.run("?CONTROLLER,ON,20")
            w.run("?DEBUG,ETM,OFF")
            w.run("?WDP,AUTOJOIN,ON")
    if f"Controller peer ID set to {X}." not in on:
        bad.append(f"?CONTROLLER,ON,{X} printed {on}")
    if _has(joined, f"temporarily joined WCB{X}") or _has(joined, f"[PEER] WCB{X} registered (live, TEMPORARY)."):
        bad.append("W1 adopted its controller as a temporary peer")
    seq1 = _seq_of(acked, t1)
    if not seq1 or f"[ETM] ACK received from WCB{X} for seq {seq1}" not in acked or f"[ETM] Seq {seq1} fully acknowledged" not in acked:
        bad.append("the unicast to the controller was not tracked to its ACK")
    if runs != 1:
        bad.append(f"the probe received the first unicast {runs} times")
    if not row1 or row1[:2] != (1, 1) or row1[3] != 0 or not row1[4]:
        bad.append(f"WCB{X} (special) row after the ACKed unicast: {row1}")
    lines = [line for _, line in entries]
    sent = _first(entries, rf"\[ETM\] Sent seq \d+: {t2}$")
    retries = [ts for ts, line in entries if re.search(rf"\[ETM\] Retry [123] to WCB{X} for seq \d+: {t2}$", line)]
    failed = _first(entries, rf"\[ETM\] WCB{X} failed to ACK seq \d+ after 3 retries: {t2}$")
    if _has(lines, f"[ETM] WCB{X} offline, canceling retry"):
        bad.append("W1 swept the controller offline mid-retry")
    if sent is None or len(retries) != 3 or failed is None:
        bad.append(f"after the leave: sent {sent is not None}, {len(retries)} retries, failed {failed is not None}")
    else:
        offsets = [round((r - sent) * 1000) for r in retries]
        if any(not k * spacing - 50 <= o <= k * spacing + 250 for k, o in enumerate(offsets, 1)):
            bad.append(f"retry offsets {offsets} ms at TIMEOUT {spacing}")
    if not row2 or row2[:2] != (2, 1) or row2[3] != 1:
        bad.append(f"WCB{X} (special) row after the unanswered unicast: {row2}")
    assert not bad, "; ".join(bad)


@test("etm.controller_offline_sweep", "With the probe as W1's controller and W1 at HB 4 / MISS 1, W1 sweeps the controller offline on its own (HB+1)xMISS = 5 s threshold, never sooner than ~4.85 s after its last packet; its OFFLINE and ONLINE edges alternate; a unicast sent while it is offline is neither tracked, retried nor counted (auto-join off; ~45 s)", needs=["wcb1", "probe2"], links=[])
def controller_offline_sweep(bench):
    """WCB-WP30 row 2. processETMHeartbeats (WCB.ino) sweeps the controller (special peer) with boardSweepOffline
    against this board's own threshold, and etmAddToPendingTable skips an offline board even when it is the controller,
    so such a unicast gets one try. The plan watches NaviCore, but its 0.5 Hz rc_hb (rc_telemetry.h) keeps it online
    under a 5 s threshold; a probe stands in as the controller (see etm.controller_unicast_retry) and, heartbeating
    about every 10 s (WCB_Client), drops offline between heartbeats. Only lines ?DEBUG,ETM prints count as packets (a WDP
    advert refreshes presence silently), so a measured gap can only be longer than the real one: the 4.85 s floor
    (etm.offline_detection_timing) cannot fail spuriously. A unicast that raced the probe's next packet (an ONLINE line
    before its Sent line) went while it was online; the next edge is tried instead, and the row allows for it."""
    X = 15
    w = usb_wcb(bench)
    _require_id_free(w, X)
    off_rx = rf"\[ETM\] WCB{X} \(special peer\) went OFFLINE \(no heartbeat for 5s\)"
    bad = []
    result, raced = None, 0
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1]:
            raise Skip("controller 20 is not enabled on W1")
        orig = _etm(before[1])
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            w.run("?DEBUG,ETM,ON")
            w.run(f"?CONTROLLER,ON,{X}")
            w.run("?ETM,HB,4")
            w.run("?ETM,MISS,1")
            w.run("?STATS,RESET")
            wm = w.dev.mark()
            with _joined(bench, "probe2", X):
                _await_line(w, rf"\[ETM\] WCB{X} came ONLINE", wm)
                since = wm
                for _ in range(3):
                    w.dev.expect(off_rx, timeout=20, since=since)
                    om = w.dev.mark()
                    t = marker("o")
                    w.send(f";W{X},{t}")
                    time.sleep(2.5)
                    after = [x.rstrip() for x in w.dev.since(om)]
                    i = next((k for k, x in enumerate(after) if re.search(rf"\[ETM\] Sent seq \d+: {t}$", x)), None)
                    if i is not None and not any(f"[ETM] WCB{X} came ONLINE" in x for x in after[:i]):
                        result = (t, after)
                        break
                    raced += 1
                    since = w.dev.mark()
                row = _etm_row(_stats(w), X, special=True)
                time.sleep(15)                      # more edges for the alternation check
                entries = _timed(w.dev, wm)
        finally:
            _restore_etm(w, orig, ("HB", "MISS"))
            w.run("?CONTROLLER,ON,20")
            w.run("?DEBUG,ETM,OFF")
            w.run("?WDP,AUTOJOIN,ON")
            w.run("?WDP,POLL")
            time.sleep(2)
    if result is None:
        bad.append("the controller came back online before each of three unicasts meant for its offline window")
    else:
        t, after = result
        seq = _seq_of(after, t)
        if any(re.search(rf"\[ETM\] Retry [123] to WCB{X} for seq {seq}:", x) for x in after) \
                or _has(after, f"[ETM] WCB{X} failed to ACK seq {seq} "):
            bad.append("a unicast sent while the controller was offline was retried")
        if f"[ETM] Seq {seq} resolved" not in after and f"[ETM] Seq {seq} fully acknowledged" not in after:
            bad.append("the unicast sent while the controller was offline never resolved")
    if not row or row[0] > raced or row[1] != row[0]:
        bad.append(f"WCB{X} (special) row {row}: only the {raced} unicast(s) that raced an ONLINE edge may count")
    pkt = re.compile(rf"\[ETM\] (Heartbeat from WCB{X}|Boot announce from WCB{X}|Received seq \d+ from WCB{X}|"
                     rf"ACK received from WCB{X}|ACK for unknown seq \d+ from WCB{X})(?!\d)")
    gaps, last, state, unpaired = [], None, None, []
    for ts, line in entries:
        if re.search(rf"\[ETM\] WCB{X} \(special peer\) went OFFLINE", line):
            if last is not None:
                gaps.append(round(ts - last, 2))
            if state == "off":
                unpaired.append(f"OFFLINE at {ts:.2f}")
            state = "off"
        elif re.search(rf"\[ETM\] WCB{X} came ONLINE", line):
            if state == "on" and "(boot)" not in line:       # a boot announce re-prints ONLINE whatever the state
                unpaired.append(f"ONLINE at {ts:.2f}")
            state, last = "on", ts
        elif pkt.search(line):
            last = ts
    bench.note(f"controller offline gaps {gaps} s; unicasts that raced an ONLINE edge: {raced}")
    if not gaps:
        bad.append("the controller never went offline with HB 4 / MISS 1")
    elif min(gaps) < 4.85:
        bad.append(f"offline sooner than the 5 s threshold: gaps {gaps}")
    if unpaired:
        bad.append(f"the controller's edges do not alternate (tracker #80's race): {unpaired}")
    assert not bad, "; ".join(bad)


@test("etm.forget_clears_pending", "?WDP,FORGET of a silent temporary peer while a broadcast waits on its ACK drops it from that broadcast: after 'Retry 1' to it there is no retry 2 or 3 and no failure line, and the broadcast resolves at the next scan (~35 s)", needs=["wcb1", "probe2"], links=[])
def forget_clears_pending(bench):
    """WCB-WP30 row 5. removeActivePeer (WCB.ino) - behind ?WDP,FORGET, a temporary peer's eviction and a WCBQ reduction -
    calls etmClearPeerFromPending, which drops the peer from every in-flight entry's expected-ACK set (and counts one
    failure, which no ?STATS row shows once the peer is gone). The probe joins as a temporary client and leaves (MESH
    LEAVE reboots it) while W1 still counts it online - eviction is 50 s away - so W1's next broadcast waits on its ACK.
    ?ETM,TIMEOUT,1500 leaves 1.5 s between retry 1 and retry 2 for the FORGET, sent once 'Retry 1' shows.
    wdp.temp_probe_lifecycle is the control: there the same kind of broadcast retries 3 times and fails."""
    w = usb_wcb(bench)
    _require_id_free(w)
    t = f"hilbc{nonce().lower()}"            # a plain broadcast reaches every broadcast port on both WCBs: inert text
    bad = []
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        try:
            w.run("?ETM,TIMEOUT,1500")
            w.run("?DEBUG,ETM,ON")
            wm = w.dev.mark()
            with _joined(bench, "probe2", 15, forget=False) as probe:
                _await_line(w, r"\[ETM\] WCB15 came ONLINE", wm)
                probe.mesh_leave()
            bm = w.dev.mark()
            w.send(t)
            seq = w.dev.expect(rf"\[ETM\] Sent seq (\d+): {t}$", timeout=3, since=bm).group(1)
            try:
                w.dev.expect(rf"\[ETM\] Retry 1 to WCB15 for seq {seq}: {t}$", timeout=4, since=bm)
            except AssertionError:
                raise Skip("the broadcast did not wait on WCB15's ACK (was it still online to W1?)") from None
            fm = w.dev.mark()
            w.send("?WDP,FORGET,15")
            time.sleep(4.5)
            lines, after = [x.rstrip() for x in w.dev.since(bm)], [x.rstrip() for x in w.dev.since(fm)]
        finally:
            w.run("?WDP,FORGET,15")                # a failed run's leftover; a no-op otherwise
            _restore_etm(w, orig, ("TIMEOUT",))
            w.run("?DEBUG,ETM,OFF")
            for k in remote_wcbs(bench):
                w.send(f";W{k},?WDP,FORGET,15")
            time.sleep(1.0)
    if "[WDP] forgot WCB15" not in after:
        bad.append("?WDP,FORGET,15 did not confirm")
    if any(re.search(rf"\[ETM\] Retry [23] to WCB15 for seq {seq}:", x) for x in lines):
        bad.append("W1 kept retrying the forgotten peer")
    if _has(lines, f"[ETM] WCB15 failed to ACK seq {seq} "):
        bad.append("W1 printed a failure for the forgotten peer")
    if f"[ETM] Seq {seq} resolved" not in after and f"[ETM] Seq {seq} fully acknowledged" not in after:
        bad.append("the broadcast did not resolve after the FORGET")
    assert not bad, "; ".join(bad)


@test("wdp.autojoin_two_advert_vetting", "Auto-join acts only on a sender's SECOND advert: 'temporarily joined WCB15' comes at least ~1 s after 'learned WCB15' (a client's boot-burst adverts are 1.3 s apart) (~20 s)", needs=["wcb1", "probe2"], links=[])
def autojoin_two_advert_vetting(bench):
    """WCB-WP48 row 1. wdpOnAdvertReceived (WCB_WDP.cpp) joins only once wcbPeerAdvertCount reaches 2, so a single
    stray or echoed packet cannot inject a peer; ?WDP,FORGET resets the count first (removeActivePeer, WCB.ino). A
    WCB_Client sends its boot burst 0.3 s after setIdentity and then every 1.3 s (_wdpTick, WCB_Client.cpp), so the join
    trails the learn by one advert whichever arrives first. A strict version needs a probe verb that sends exactly one
    advert (WCB-WP59)."""
    w = usb_wcb(bench)
    _require_id_free(w)
    if not _has(w.run("?WDP,AUTOJOIN"), "[WDP] auto-join is ON"):
        raise Skip("auto-join is not ON on W1")
    w.run("?WDP,FORGET,15")
    wm = w.dev.mark()
    with probe_in_mesh(bench, "probe2", 15):
        _await_line(w, r"\[WDP\] temporarily joined WCB15\b", wm)
        entries = _timed(w.dev, wm)
    learned = _first(entries, r"\[WDP\] learned WCB15\b")
    joined = _first(entries, r"\[WDP\] temporarily joined WCB15\b")
    assert learned is not None, "no 'learned WCB15' line"
    gap = round(joined - learned, 2)
    bench.note(f"'temporarily joined' came {gap} s after 'learned'")
    assert 1.0 <= gap <= 4.0, f"joined {gap} s after the first advert, expected one boot-burst advert later (~1.3 s)"


@test("etm.learned_unreciprocated_not_expected", "A learned peer that has never ACKed W1 is left out of a broadcast's expected ACKs (no wait, no retry, its row stays at Sent 0); its ACK to that broadcast, even one arriving after W2's resolved it, makes the next broadcast expect and count it; a unicast to it is always expected (probe as a permanent client at 13; ~40 s)", needs=["wcb1", "probe2"], links=[])
def learned_unreciprocated_not_expected(bench):
    """WCB-WP19 row 2 and tracker #96. etmAddToPendingTable (WCB.ino) skips a learned peer with no
    wcbPeerReciprocated on a broadcast (the mixed-fleet guard: it may be a phantom), and etmProcessAck sets
    wcbPeerReciprocated for any ACK from a learned peer. W2's ACK alone resolves the first broadcast, so the probe's
    ACK often arrives after it ('ACK for unknown seq'); until #96 that ACK promoted nothing, and a peer that was only
    ever broadcast to was never expected, so never retried. Either way the race goes (noted), the second broadcast
    must expect and count the probe. Then a unicast, which always expects a learned peer. The permanent probe is
    learned by W2 and NaviCore too, so all three forget it."""
    n = 13
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _require_id_free(w, n)
    if not _has(w.run("?WDP,AUTOJOIN"), "[WDP] auto-join is ON"):
        raise Skip("auto-join is not ON on W1")
    t1, t2, u = f"hilbc{nonce().lower()}", f"hilbc{nonce().lower()}", marker("u")
    bad = []
    promoted, ulines, runs = None, [], None
    w.run(f"?WDP,FORGET,{n}")                    # a clean advert count
    try:
        w.run("?DEBUG,ETM,ON")
        wm = w.dev.mark()
        with _joined(bench, "probe2", n, temporary=False, forget=False) as probe:
            _await_line(w, rf"\[WDP\] auto-joined WCB{n} ", wm)
            _await_line(w, rf"\[ETM\] WCB{n} came ONLINE", wm)
            w.run("?STATS,RESET")
            row0 = _etm_row(_stats(w), n)
            bm = w.dev.mark()
            w.send(t1)
            time.sleep(2.0)
            l1, row1 = [x.rstrip() for x in w.dev.since(bm)], _etm_row(_stats(w), n)
            s1 = _seq_of(l1, t1)
            promoted = s1 is not None and f"[ETM] ACK received from WCB{n} for seq {s1}" in l1
            late = s1 is not None and any(x.startswith(f"[ETM] ACK for unknown seq {s1} from WCB{n}") for x in l1)
            mid = _etm_row(_stats(w), n)
            bm = w.dev.mark()
            w.send(t2)
            time.sleep(2.0)
            l2, row2 = [x.rstrip() for x in w.dev.since(bm)], _etm_row(_stats(w), n)
            um, pm = w.dev.mark(), probe.dev.mark()
            w.send(f";W{n},{u}")
            runs = _probe_rx(probe, pm, u, me)
            time.sleep(0.5)
            ulines = [x.rstrip() for x in w.dev.since(um)]
            probe.mesh_leave()
    finally:
        w.run("?DEBUG,ETM,OFF")
        _forget_everywhere(bench, n, navicore=True)
    bench.note(f"WCB{n}'s ACK to the first broadcast: {'in time' if promoted else 'after W2 resolved it' if late else 'none seen'}")
    if not row0 or not row0[4]:
        bad.append(f"WCB{n} was not an online learned peer before the first broadcast: {row0}")
    if not s1:
        bad.append("no 'Sent seq' line for the first broadcast")
    else:
        if any(re.search(rf"\[ETM\] Retry [123] to WCB{n} for seq {s1}:", x) for x in l1) or _has(l1, f"[ETM] WCB{n} failed to ACK seq {s1} "):
            bad.append("the first broadcast waited on the unreciprocated learned peer")
        if f"[ETM] Seq {s1} fully acknowledged" not in l1 and f"[ETM] Seq {s1} resolved" not in l1:
            bad.append("the first broadcast did not resolve")
    if not row1 or row1[:4] != (0, 0, 0, 0):
        bad.append(f"WCB{n} row after the first broadcast: {row1} (it was expected)")
    if s1 and not promoted and not late:
        bad.append(f"no ACK from WCB{n} for the first broadcast (seq {s1}) under ?DEBUG,ETM")
    s2 = _seq_of(l2, t2)
    if not mid or not row2 or row2[0] != mid[0] + 1 or row2[1] != mid[1] + 1:
        bad.append(f"the second broadcast did not expect and count WCB{n}: {mid} -> {row2}"
                   f"{' (its first ACK came after W2 resolved the entry: tracker #96)' if late else ''}")
    if not s2 or f"[ETM] Seq {s2} fully acknowledged" not in l2:
        bad.append("the second broadcast was not fully acknowledged")
    su = _seq_of(ulines, u)
    if runs != 1 or not su or f"[ETM] Seq {su} fully acknowledged" not in ulines:
        bad.append(f"the unicast: received {runs} time(s), acknowledged {bool(su and f'[ETM] Seq {su} fully acknowledged' in ulines)}")
    assert not bad, "; ".join(bad)


@test("wdp.autojoin_permanent_downgrade", "A non-temporary client heard twice is auto-joined as a PERSISTED learned peer (DUMP PEER=2, ?PEERSLIVE +1, ;W12 reaches it and is ACKed, a member again after a W1 reboot); advertising TEMPORARY later downgrades it (PEER=4) and un-persists it, so the next reboot does not restore it (2 W1 reboots; ~2.5 min)", needs=["wcb1", "probe2"], links=[])
def autojoin_permanent_downgrade(bench):
    """WCB-WP19 row 1. wdpOnAdvertReceived (WCB_WDP.cpp) counts a sender's adverts and on the second one joins a
    non-temporary device with addActivePeer(id, learned) - 'auto-joined', persisted by saveLearnedPeers 5 s later
    (LEARNED_FLUSH_DEBOUNCE_MS) - and one advertising TEMPORARY with addTemporaryPeer, which clears a learned bit
    ('downgraded to temporary'); loadLearnedPeers restores the mask at boot (WCB.ino). The probe is id 12 and only
    receives (sharing the id is fine, s19 MESH_IDS). W2 and NaviCore learn it too, so the cleanup forgets it on every WCB
    and sends NaviCore FORGET_PEER. The second join goes past probe_in_mesh, which refuses a PEER=2 id. The expected
    restore counts come from the baseline ?PEERSLIVE: live = WCBQ floor peers + learned + temporary."""
    n = 12
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _require_id_free(w, n)
    if not _has(w.run("?WDP,AUTOJOIN"), "[WDP] auto-join is ON"):
        raise Skip("auto-join is not ON on W1")
    n0, line0 = _live_peers(w)
    floor = int(re.search(r"WCBQ floor (\d+)", line0).group(1))
    temps0 = sum(1 for x in _dump(w) if x.startswith("[WDP:N=") and x.endswith(",PEER=4]"))
    learned0 = n0 - temps0 - (floor - 1 if me <= floor else floor)
    t = marker("p")
    bad = []
    w.run(f"?WDP,FORGET,{n}")                    # a clean advert count, and no ESP-NOW peer left: both join lines print
    try:
        w.run("?DEBUG,ETM,ON")
        wm = w.dev.mark()
        with _joined(bench, "probe2", n, temporary=False, forget=False) as probe:
            _await_line(w, rf"\[WDP\] auto-joined WCB{n} ", wm)
            _await_line(w, rf"\[ETM\] WCB{n} came ONLINE", wm)
            lines = [x.rstrip() for x in w.dev.since(wm)]
            steps = [f"[WDP] learned WCB{n} HILProbe", f"[PEER] WCB{n} registered (live, learned).",
                     f"[WDP] auto-joined WCB{n} HILProbe (client)"]
            idx = [next((i for i, x in enumerate(lines) if s in x), -1) for s in steps]
            if -1 in idx or idx != sorted(idx):
                bad.append(f"join lines missing or out of order: {dict(zip(steps, idx))}")
            if _field(_row(_dump(w), n), "PEER") != "2":
                bad.append(f"DUMP PEER={_field(_row(_dump(w), n), 'PEER')} after the join, expected 2")
            if _live_peers(w)[0] != n0 + 1:
                bad.append(f"?PEERSLIVE {_live_peers(w)[0]} after the join, expected {n0 + 1}")
            pm, am = probe.dev.mark(), w.dev.mark()
            w.send(f";W{n},{t}")
            runs = _probe_rx(probe, pm, t, me)
            time.sleep(0.5)
            acked = [x.rstrip() for x in w.dev.since(am)]
            seq = _seq_of(acked, t)
            if runs != 1 or not seq or f"[ETM] Seq {seq} fully acknowledged" not in acked:
                bad.append(f";W{n}: received {runs} time(s), acknowledged {bool(seq and f'[ETM] Seq {seq} fully acknowledged' in acked)}")
            time.sleep(6)                          # past the 5 s learned-peer flush
            probe.mesh_leave()
        boot = [x.rstrip() for x in w.dev.since(w.reboot())]
        if _restored(boot) != learned0 + 1:
            bad.append(f"the first reboot restored {_restored(boot)} learned peer(s), expected {learned0 + 1}")
        if _has(w.run(f";W{n},?PEERSLIVE"), f"WCB {n} is not a reachable target"):
            bad.append(f"WCB{n} is not a member after the reboot")
        if _live_peers(w)[0] != n0 - temps0 + 1:
            bad.append(f"?PEERSLIVE {_live_peers(w)[0]} after the first reboot, expected {n0 - temps0 + 1}")
        dm = w.dev.mark()
        with _joined(bench, "probe2", n, temporary=True, forget=False) as probe:
            _await_line(w, rf"\[WDP\] downgraded to temporary WCB{n} HILProbe \(temporary\)", dm)
            if _field(_row(_dump(w), n), "PEER") != "4":
                bad.append(f"DUMP PEER={_field(_row(_dump(w), n), 'PEER')} after the downgrade, expected 4")
            time.sleep(6)                          # the removal's own 5 s flush
            probe.mesh_leave()
        boot2 = [x.rstrip() for x in w.dev.since(w.reboot())]
        if _restored(boot2) != learned0:
            bad.append(f"the second reboot restored {_restored(boot2)} learned peer(s), expected {learned0}")
        if not _has(w.run(f";W{n},x"), f"WCB {n} is not a reachable target"):
            bad.append(f"WCB{n} is still a member after the downgrade and a reboot")
        if _live_peers(w)[0] != n0 - temps0:
            bad.append(f"?PEERSLIVE {_live_peers(w)[0]} after the second reboot, expected {n0 - temps0}")
    finally:
        w.run("?DEBUG,ETM,OFF")
        _forget_everywhere(bench, n, navicore=True)
        _peers_back(w)
    assert not bad, "; ".join(bad)


@test("wdp.neighbor_stale_after_ttl", "A WDP neighbour silent for over 180 s keeps its row but goes stale: DUMP SEEN=0, LIST 'stale', ?WDP,15 '(stale)' (a probe learned with auto-join off, so never joined or evicted; slow, ~200 s)", needs=["wcb1", "probe2"], links=[])
def neighbor_stale_after_ttl(bench):
    """WCB-WP48 row 2. wdpTick (WCB_WDP.cpp) clears a row's `confirmed` flag once its last advert is WDP_TTL_MS (180 s)
    old and keeps the slot as topology memory; printWdpDump shows SEEN=0, printWdpList 'stale' and printWdpDetail
    '(stale)'. With auto-join off the probe is learned but never becomes a temporary peer, so the 50 s eviction that
    drops a temporary peer's row never applies. Auto-join is off only while the probe advertises: only an advert can
    join it, and it is silent once it has left. W2 (auto-join on) does adopt it, so W2 forgets it at once."""
    w = usb_wcb(bench)
    _require_id_free(w)
    bad = []
    with config_guard(bench, 1):
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            wm = w.dev.mark()
            with _joined(bench, "probe2", 15, forget=False) as probe:
                _await_line(w, r"\[WDP\] learned WCB15\b", wm)
                time.sleep(3)                      # the rest of its boot burst
                probe.mesh_leave()
            left = time.monotonic()
            w.run("?WDP,AUTOJOIN,ON")
            for k in remote_wcbs(bench):
                w.send(f";W{k},?WDP,FORGET,15")
            fresh = _row(_dump(w), 15)
            time.sleep(max(0.0, left + 188 - time.monotonic()))
            dump = _dump(w)
            listing = [x.rstrip() for x in w.run("?WDP,LIST")]
            detail = [x.rstrip() for x in w.run("?WDP,15")]
            lines = [x.rstrip() for x in w.dev.since(wm)]
        finally:
            w.run("?WDP,AUTOJOIN,ON")
            w.run("?WDP,FORGET,15")
    stale = _row(dump, 15)
    if _field(fresh, "SEEN") != "1" or _field(fresh, "PEER") != "0":
        bad.append(f"row right after the probe left: {fresh}")
    if stale is None:
        bad.append("the silent neighbour's row was dropped")
    elif _field(stale, "SEEN") != "0" or int(_field(stale, "AGE") or 0) < 180 or _field(stale, "PEER") != "0":
        bad.append(f"row after 188 s of silence: {stale}")
    if not any(re.match(r"^15\s{3}.*\sstale$", x) for x in listing):
        bad.append(f"?WDP,LIST has no stale row 15: {[x for x in listing if x.startswith('15 ')]}")
    if '==== Device 15  "HILProbe" ====' not in detail or not any(re.match(r"^  Last advert : \d+s ago  \(stale\)$", x) for x in detail):
        bad.append(f"?WDP,15 detail: {detail}")
    if _has(lines, "temporarily joined WCB15") or _has(lines, "[PEER] WCB15 registered") or _has(lines, "temporary WCB15 evicted"):
        bad.append("W1 joined or evicted the probe with auto-join off")
    assert not bad, "; ".join(bad)
