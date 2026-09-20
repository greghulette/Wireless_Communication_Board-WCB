"""ETM settings and delivery, ?STATS, peers and the controller, aliases, the command characters, WDP.

Built from the verified etm_config_wdp specs; console literals re-checked with grep -a (WCB.ino holds a NUL byte, so
ripgrep silently skips it). Rules from the specs:
- Only W1's ETM settings are changed. ETM, CHKSM, MAC, EPASS or WCBCH changes on mesh-only-reachable W2 strand it.
- '?MAC,3,<other>' deafens W1 at once (its receive filter changes, its radio address only at boot) and writes NVS:
  it is restored in a finally, as the first command, and W1 is never reset in that window.
- An ETM ACK does not mean execution (WCB.ino:4269-4272): delivery is asserted on the probe wire.
- WCB.run() breaks while CMDCHAR is not ';', the delimiter is ',', or the LFI is ';'; those windows use dev.send.
- Probe mesh ids are temporary and never 1/2/19/20 (probe_in_mesh); a temporary peer is evicted 50 s after it
  goes silent (WCB.ino:508), and until then every broadcast expects its ACK, so those tests run last.
"""
import re
import time
import zlib
from contextlib import contextmanager

from hil.runner import Skip, test
from suites.common import (Console, Watch, config_guard, link, marker, nonce, padded, probe_in_mesh, require_tokens,
                           snapshot, token, usb_wcb)

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
                  ("?etmmiss6", "ETM missed heartbeats set to 6"), ("?EtmMiss6", "Unknown command: EtmMiss6"), ("?ETMON", "ETM enabled")]
        try:
            bad += [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
            cfg = _cfg(w)
            bad += [f"?config lacks {x!r}" for x in ("Heartbeat interval:   12 sec (+/- 1)", "Boot heartbeat:       1-3 sec",
                                                     "Offline after:        6 missed heartbeats (78 sec max)", "Retry timeout:        650 ms",
                                                     "Char message count:   10  delay: 150 ms") if not _has(cfg, x)]
        finally:
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


def _require_w2_online(w):
    if not any(x.startswith("WCB2: ") and "Online" in x for x in w.run("?STATS")):
        raise Skip("W1's ?STATS does not show WCB2 online")


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
    gaps, last_seen = [], None
    for t, line in entries:
        if re.search(r"\[ETM\] (Heartbeat from WCB2|WCB2 came ONLINE)(?!\d)", line):   # not NaviCore's WCB20
            last_seen = t
        elif "[ETM] WCB2 went OFFLINE (no heartbeat for 5s)" in line and last_seen is not None:
            gaps.append(round(t - last_seen, 2))
    nexts = [int(m.group(1)) for _, line in entries for m in [re.search(r"\[ETM\] Next heartbeat in (\d+)ms", line)] if m]
    bench.note(f"offline gaps {gaps} s; next-heartbeat delays {nexts}")
    assert cfg_ok, "?config does not show '1 missed heartbeats (5 sec max)'"
    assert gaps, "W2 never went offline with HB 4 / MISS 1"
    assert min(gaps) >= 4.95 and sorted(gaps)[len(gaps) // 2] <= 6.5, f"offline gaps {gaps}"
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
    would join as a persisted learned peer. The OFF window stays under 30 s."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    bad = []
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1] or _row(_dump(w), 20) is None:
            raise Skip("controller 20 is not enabled, or NaviCore is not in W1's WDP table")
        try:
            w.run("?WDP,AUTOJOIN,OFF")
            for cmd in ("?CONTROLLER", "?SPECIAL"):
                out = [x.rstrip() for x in w.run(cmd)]
                if "Controller peer (ID 20) is currently ENABLED." not in out or "Use ?CONTROLLER,ON[,<id>] (1-20) or ?CONTROLLER,OFF" not in out:
                    bad.append(f"{cmd} printed {out}")
            for bad_id in (21, 0):
                if not _has(w.run(f"?CONTROLLER,ON,{bad_id}"), f"Invalid controller peer ID {bad_id}. Valid range: 1-20."):
                    bad.append(f"ON,{bad_id} accepted")
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
            out = w.run("?CONTROLLER,ON,20")
            if not (_has(out, "Controller peer (ID 20) ENABLED.") and _has(out, "Controller peer WCB20 registered (live).")):
                bad.append(f"ON,20 printed {out}")
            wm = w.dev.mark()
            w.send(";W20,?version")
            try:
                w.dev.expect(r"^\[TERM:20\]End of Version", timeout=5, since=wm)
            except AssertionError:
                bad.append("NaviCore did not answer after ON,20")
        finally:
            if not _has(w.run("?CONTROLLER"), "is currently ENABLED"):
                w.run("?CONTROLLER,ON,20")
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
            time.sleep(3)
            lines = [x.rstrip() for x in w.dev.since(wm)]
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


@test("chars.delim_comma_restore_path", "With delimiter ',' every comma-bearing command splits; the legacy ?D^ still restores", needs=["wcb1"], links=[])
def delim_comma_restore_path(bench):
    """Under a second: mesh-received comma commands also split and broadcast fragments meanwhile.

    Each line waits for the previous reply. A USB line is split on the delimiter when it is READ (serialCommandTask ->
    parseCommandsAndEnqueue, WCB.ino:2199), not when it runs, so lines sent back to back were all split on the old '^'."""
    w = usb_wcb(bench)
    t = marker()
    with config_guard(bench, 1):
        try:
            _sent(w, "?DELIM,,", r"^Delimiter updated to: ','")
            m = w.dev.mark()
            w.dev.send(f";S0A{t},;S0B{t}")              # prints on USB only; runs both halves only if ',' splits
            w.dev.expect(rf"^B{t}$", timeout=3, since=m)
            split = f"A{t}" in [x.strip() for x in w.dev.since(m)]
            _sent(w, "?D^", r"^Command delimiter updated to: '\^'")   # the legacy spelling holds no ','
        finally:
            _restore_delim(w)
    assert split, "under ',' a comma chain did not run both halves"


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
    cap |= 0x0008 if "?KYBER,LOCAL" in up else 0
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
        if "Capability codes: M=Maestro host  R=Maestro remote  K=Kyber  H=HCR  3=MP3  W=WLED  P=PWM  C=Controller link" not in out:
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
    # Never probe unknown subcommands starting ADD/FORGET/CLEAR/AUTOJOIN/DETAIL or a digit: they are prefix-matched.
    if not _has(w.run("?WDP,FOO"), "[WDP] unknown subcommand 'FOO' (LIST | <n> | DETAIL,n | STATUS | DUMP | DA | POLL | ON | OFF | AUTOJOIN[,ON|,OFF] | ADD,<id> | FORGET,<id> | CLEAR)"):
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
    poll = w.run("?WDP,POLL")
    time.sleep(3)
    dump = _dump(w)
    assert _has(poll, "[WDP] polled: advertised + solicited the mesh"), poll
    late = {n: _field(_row(dump, n), "AGE") for n in (2, 20) if _row(dump, n) and int(_field(_row(dump, n), "AGE")) > 3}
    assert not late, f"not refreshed by the poll: {late}"


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


@test("wdp.da_announce_propagates", "An @WDP1 announce on an unlabelled W2 port reaches W1's DUMP through W2's on-change advert and ages out after 90 s (slow, ~100 s)", needs=["wcb1"], links=["W2S3", "W2S4", "W2S5"])
def da_announce_propagates(bench):
    w = usb_wcb(bench)
    tokens2 = snapshot(bench, 2)
    port = next((p for p in ("S4", "S3", "S5") if bench.links.usable(2, p, send=True) and not token(tokens2, f"?LABEL,{p},")), None)
    if port is None:
        raise Skip("no wired, unlabelled W2 port among S3-S5")
    l = link(bench, 2, port)
    with Console(bench, 2) as c2:
        cm = c2.mark()
        sent_at = time.monotonic()
        l.send(b'@WDP1 {"type":"HILDev","fw":"9.9"}\r\n')
        time.sleep(3)
        announce, dump = c2.lines(cm), _dump(w)
        da = _crun(c2, "?WDP,DA", 1.0)
        time.sleep(max(0.0, 95 - (time.monotonic() - sent_at)))
        stopped_after = next((round(ts - sent_at, 1) for ts, x in c2.dev.lines[cm:] if f"[WDP-DA] {port}: HILDev stopped announcing" in x), None)
        dump2 = _dump(w)
    bench.note(f"WDP-DA announce on W2 {port} expired {stopped_after} s after it was sent")
    assert _has(announce, f"[WDP-DA] {port}: HILDev fw 9.9"), "W2 did not log the announce"
    assert f"[WDPIF:N=2,S={port[1]},DEV=HILDev]" in dump, "W1's DUMP lacks the announced device"
    assert _has(da, "Serial-attached devices (WDP-DA announces):") and any(x.strip().startswith(f"{port}  HILDev") and "fw 9.9" in x for x in da), da
    assert stopped_after is not None and 88 <= stopped_after <= 94, f"'stopped announcing' after {stopped_after} s (expected ~90-92)"
    assert not any(x.startswith(f"[WDPIF:N=2,S={port[1]},") for x in dump2), "the expired device is still in W1's DUMP"
