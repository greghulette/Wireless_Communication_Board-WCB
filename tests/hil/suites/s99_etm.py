"""ETM: reboot announce and network characterisation. Slow — reboots boards and loads the mesh.

The deferred-restart tests (tracker #93) reboot W1 once each and move nothing: W2 re-arms W1's remote terminal or
reports stats to it, and W1's own console asks for its version. Restore if aborted: `;W1,?RTERM,STOP` on W2's console
(the session and ?DEBUG,MGMT are RAM only, and W1's queued ?reboot clears both anyway).

etm.seq_wrap (opt-in etm_seq_wrap, WCB-WP46) floods the mesh from W1 for several minutes to take its sequence counter
past 65,535. Restore if aborted: `?DEBUG,ETM,OFF` on W1 and W2, and W1's `?BCAST,OUT,S<n>,ON` for each port its chain
had on (the test turns them off for the flood).
"""
import re
import threading
import time

from hil.runner import test
from hil.wcb import WCB
from hil.runner import Skip
from suites.common import (Watch, config_guard, link, marker, nonce, probe_in_mesh, remote_wcbs, require_tokens,
                           snapshot, token, usb_wcb)


@test("etm.remote_reboot", "W2 rebooted over the mesh announces itself and answers again", needs=["wcb1"])
def remote_reboot(bench):
    w = usb_wcb(bench)
    m = w.send(";W2,?reboot")
    w.dev.expect(r"^\[ETM\] WCB2 came ONLINE", timeout=25, since=m)
    time.sleep(3)
    snapshot(bench, 2)


@test("etm.w1_reboot_sees_peers", "After a W1 reboot, W1 sees W2 come back online", needs=["wcb1"])
def w1_reboot(bench):
    w = usb_wcb(bench)
    m = w.reboot()
    w.dev.expect(r"^\[ETM\] WCB2 came ONLINE", timeout=25, since=m)


# ============================================================ deferred restart under traffic (tracker #93)
QUIET_S = 4.0     # PWM_REBOOT_QUIET_MS (WCB.ino): no command processed for this long, then the restart
CAP_S = 20.0      # RESTART_MAX_DEFER_MS (WCB.ino): the restart goes this long after the request, whatever arrives
LIMIT_S = 10.0    # the quiet window plus margin, for the exempt streams: half the cap, so a stream that still holds
                  # the window open fails here instead of passing at the cap


def _at(dev, pattern, since):
    """Host arrival time (time.monotonic) of the first line at or after `since` matching `pattern`, or None."""
    rx = re.compile(pattern)
    return next((t for t, text in list(dev.lines[since:]) if rx.search(text)), None)


def _window(dev, since):
    """The lines W1 printed between 'Reboot queued' and 'Rebooting now' (to the end if it has not restarted)."""
    lines = [text for _, text in list(dev.lines[since:])]
    a = next((i for i, x in enumerate(lines) if x.startswith("Reboot queued")), len(lines))
    b = next((i for i, x in enumerate(lines) if x.startswith("Rebooting now")), len(lines))
    return lines[a + 1:b]


def _reboot_while(w, send_one, limit_s):
    """Queue ?reboot on W1 and call send_one() once a second until W1 starts its restart or limit_s passes.
    Returns (mark, seconds from 'Reboot queued' to 'Rebooting now' by host arrival time, or None)."""
    m = w.send("?reboot")
    w.dev.expect(r"^Reboot queued", timeout=3, since=m)
    deadline = time.monotonic() + limit_s
    while _at(w.dev, r"^Rebooting now", m) is None and time.monotonic() < deadline:
        time.sleep(1.0)
        send_one()
    queued, went = _at(w.dev, r"^Reboot queued", m), _at(w.dev, r"^Rebooting now", m)
    return m, (went - queued if went is not None else None)


HEAP_RX = re.compile(r"Free heap\s*:\s*(\d+) bytes \(min since boot (\d+)\)")     # WCB_WiFi.cpp, ?WIFI's status
LOAD_DONE = "ETM load test complete:"                                                # processETMLoad's closing line
NOT_SENT = re.compile(r"^\[MESH\] (\d+) ESP-NOW frame\(s\) not sent")             # wcbEspNowReportDrops
TX_SETTLE_S = 3.0     # after a generator ends, its last given-up frames are still being reported
TX_WATCH_S = 12.0     # then a heartbeat period (HB 10 +/- 1 s) in which a working board gives no frame up


def _heaps(bench):
    """{board: (free heap, lowest since boot)} from ?WIFI on every WCB with its own USB cable."""
    out = {}
    for n, dev in sorted(bench.usb_wcbs().items()):
        for x in WCB(bench.dev(dev)).run("?WIFI", timeout=6):
            m = HEAP_RX.search(x)
            if m:
                out[n] = (int(m.group(1)), int(m.group(2)))
                break
    return out


def _heap_note(bench, test_id, label):
    """Note every USB WCB's free heap (lowest since boot) -> the readings. ESP-NOW's driver buffers come from it, and an
    AP-mode board has about 17 KB (W1) or 13 KB (W2, with WLED) free: the readings around a load show whether one
    ran out (full run 20261006-122850, W2's wedged transmit)."""
    h = _heaps(bench)
    bench.note(f"{test_id}: free heap {label}: " + ", ".join(f"W{n} {f} (min {m})" for n, (f, m) in h.items()))
    return h


def _tx_after_load(bench, w, since, problems, facts):
    """After a ?ETM,CHAR's load: once every other USB WCB's generator has ended (its LOAD_DONE line since its mark in
    `since`), each must transmit again - its ';W1,;S0,<marker>' reaches W1's console - and give no frame up
    (NOT_SENT) over TX_WATCH_S after TX_SETTLE_S. In full run 20261006-122850 W2's transmit wedged during
    etm.char_per_board_clamp's load: every frame was given up, heartbeats included, for 2 min until a reboot, and W1
    and W3 called W2 offline. Problems are added; facts get each board's last report."""
    peers = {n: WCB(bench.dev(dev)) for n, dev in bench.usb_wcbs().items() if n != bench.usb_wcb_number()}
    deadline = time.monotonic() + 20.0
    for n, wn in peers.items():
        while not any(x.startswith(LOAD_DONE) for x in wn.dev.since(since[n])) and time.monotonic() < deadline:
            time.sleep(0.5)
    time.sleep(TX_SETTLE_S)
    after = {n: wn.dev.mark() for n, wn in peers.items()}
    for n, wn in peers.items():
        tag = marker("TX")
        m1 = w.dev.mark()
        wn.dev.send(f";W1,;S0,{tag}")
        try:
            w.dev.expect(rf"{tag}$", timeout=6, since=m1)
        except AssertionError:
            problems.append(f"W{n} could not reach W1 after the load (';W1,;S0,<marker>' never arrived)")
    time.sleep(TX_WATCH_S)
    for n, wn in peers.items():
        given_up = [x.rstrip() for x in wn.dev.since(after[n]) if NOT_SENT.match(x)]
        facts[f"W{n}_given_up_after_load"] = len(given_up)
        if given_up:
            problems.append(f"W{n} still gave frames up {TX_SETTLE_S:.0f}-{TX_SETTLE_S + TX_WATCH_S:.0f} s after its "
                            f"load ended ({len(given_up)} report(s), the last {given_up[-1]!r}): its ESP-NOW transmit "
                            f"is wedged")


def _peers_online(bench, w, wait=25.0, strict=True):
    """Wait until W1's ETM table shows every other bench WCB online -> the seconds it took. A WCB that has just booted
    treats every peer as offline until that peer's next packet, a whole heartbeat away at worst (HB 10 +/- 1 s): nothing
    answers a boot announce (the heartbeat branch of espNowReceiveCallback, WCB.ino). Until then ?ETM,CHAR aborts with
    'no online peers' (etm.char_unicast, run 20260924-234056) and a unicast to the peer goes out once with no ETM retry.
    ?WDP,POLL has every peer advertise within about a second, and any packet marks its sender online. strict=False only
    notes a peer still offline, for a finally that must not replace the test's own failure."""
    others, t0 = remote_wcbs(bench), time.monotonic()
    w.run("?WDP,POLL")
    time.sleep(1.0)     # the adverts it solicits print '[ETM] WCBn came ONLINE' on W1's WiFi task 77-654 ms later (F18);
                        # a ?STATS sent at once prints into that window (run 20260925-092255)
    while True:
        stats = w.etm_board_stats()
        off = [n for n in others if not stats.get(n, {}).get("online")]
        if not off or time.monotonic() - t0 >= wait:
            break
        time.sleep(1.0)
    if off and strict:
        raise AssertionError(f"W1 does not see WCB{off} online {wait:.0f} s after ?WDP,POLL")
    if off:
        bench.note(f"W1 still saw WCB{off} offline {wait:.0f} s after its reboot")
    return time.monotonic() - t0


def _finish_reboot(bench, w, m):
    """Let a queued ?reboot run out (nothing is sending any more, so the window elapses) and W1 boot, then leave the
    bench as found: W1 seeing its peers online."""
    w.dev.expect(r"^Rebooting now", timeout=CAP_S + 8, since=m)
    w.wait_boot(m, timeout=30)
    try:
        _peers_online(bench, w, strict=False)
    except AssertionError as e:     # ExpectTimeout is one: a failed read here must not replace the test's result
        bench.note(f"after the reboot the peer check failed: {(str(e).splitlines() or [repr(e)])[0]}")


@test("etm.reboot_rterm_rearm", "A ?reboot queued on W1 restarts within the 4 s quiet window plus margin (10 s; the cap is 20 s) while W2 re-arms W1's remote terminal to itself every second over the mesh: re-arming a running session is not activity (tracker #93; 1 reboot)", needs=["wcb1", "wcb2"])
def reboot_rterm_rearm(bench):
    """F21 (docs/HIL_TEST_AUDIT.md): every queue item restarted the quiet window, so Intellex through NaviCore,
    re-arming W1's terminal about once a second (?RTERM,START,20), held a queued ?reboot off for good (runs
    20260924-190733, -213158). W2 plays that client: ';W1,?RTERM,START,2' goes to W1 as an ETM unicast, which W1 queues
    and runs like NaviCore's. The first START opens the session and is activity; every later one re-arms the same relay
    and must not count (quietWindowExempt, WCB.ino). W1 prints '[RTERM] Session started' for each, so the re-arms that
    landed inside the window can be counted."""
    w1, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    rearm = ";W1,?RTERM,START,2"
    m2 = w2.send(rearm)
    w2.dev.expect(r"^\[TERM:1\]\[RTERM\] Session started", timeout=5, since=m2)
    m = None
    try:
        for _ in range(2):                  # the stream is already running when the reboot is queued
            time.sleep(1.0)
            w2.send(rearm)
        m, took = _reboot_while(w1, lambda: w2.send(rearm), CAP_S + 4)
        relays = [int(r.group(1)) for r in (re.search(r"Session started \S+ relay WCB(\d+)", x)
                                             for x in _window(w1.dev, m)) if r]
        bench.note(f"W1 restarted {took if took is None else round(took, 1)} s after ?reboot with {len(relays)} "
                   f"re-arm(s) in the window (relays {relays})")
        foreign = sorted({r for r in relays if r != 2})
        why = (f"; W1 was also re-armed to relay(s) {foreign} - another client of W1's terminal (Intellex through "
               f"NaviCore?) switched the session, and a switch is activity" if foreign else "")
        assert took is not None, f"W1 did not restart within {CAP_S + 4:.0f} s of ?reboot{why}"
        assert relays.count(2) >= 2, f"only {relays.count(2)} re-arm(s) from W2 reached W1 inside the window: {relays}"
        assert took <= LIMIT_S, (f"W1 restarted {took:.1f} s after ?reboot, over {LIMIT_S:.0f} s: the re-arms still "
                                 f"hold the {QUIET_S:.0f} s quiet window open{why}")
        assert took >= QUIET_S - 1.0, f"W1 restarted {took:.1f} s after ?reboot, inside its own {QUIET_S:.0f} s window"
    finally:
        if m is not None:
            _finish_reboot(bench, w1, m)
        w2.send(";W1,?RTERM,STOP")          # a re-arm sent during the restart can open a session after the boot
        time.sleep(0.5)


@test("etm.reboot_stats_rpt", "A ?reboot queued on W1 restarts within the 4 s quiet window plus margin (10 s) while W2 sends W1 a ?STATS,RPT every second: a store-only report is not activity (tracker #93; 1 reboot)", needs=["wcb1", "wcb2"])
def reboot_stats_rpt(bench):
    """F21's other exemption. NaviCore reports its delivery counters to W1 every 30 s, which alone stretched a restart
    to ~8 s. A report stores a row in RAM and prints nothing unless ?DEBUG,MGMT is on (storeReportedStats, WCB.ino),
    so the test turns that on to count the reports that landed in the window. Both are RAM only; the reboot clears
    them."""
    w1, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    rpt = ";W1,?STATS,RPT,2,0,0,0,0,0,0,0"
    w1.run("?DEBUG,MGMT,ON")
    m = None
    try:
        for _ in range(2):
            w2.send(rpt)
            time.sleep(1.0)
        m, took = _reboot_while(w1, lambda: w2.send(rpt), CAP_S + 4)
        got = sum(1 for x in _window(w1.dev, m) if x.startswith("[STATS] RPT from WCB2:"))
        bench.note(f"W1 restarted {took if took is None else round(took, 1)} s after ?reboot with {got} report(s) "
                   f"from W2 in the window")
        assert took is not None, f"W1 did not restart within {CAP_S + 4:.0f} s of ?reboot"
        assert got >= 2, f"only {got} ?STATS,RPT from W2 reached W1 inside the window"
        assert took <= LIMIT_S, (f"W1 restarted {took:.1f} s after ?reboot, over {LIMIT_S:.0f} s: the reports still "
                                 f"hold the {QUIET_S:.0f} s quiet window open")
        assert took >= QUIET_S - 1.0, f"W1 restarted {took:.1f} s after ?reboot, inside its own {QUIET_S:.0f} s window"
    finally:
        if m is None:
            w1.run("?DEBUG,MGMT,OFF")
        else:
            _finish_reboot(bench, w1, m)


@test("etm.reboot_defer_cap", "A ?reboot queued on W1 while its console sends a command every second (so the 4 s quiet window never elapses) still restarts, at the 20 s cap, after an ungated 'Restart held off' line (tracker #93; 1 reboot)", needs=["wcb1"])
def reboot_defer_cap(bench):
    """The backstop (RESTART_MAX_DEFER_MS, WCB.ino): a stream nobody exempted - here ?VERSION once a second, activity
    like a push - holds the restart off only until the cap, which is sized to outlast the tail of a legitimate push
    (see the constant's comment). pwm.map_deferred_reboot checks the other side: 9 s of commands still hold it off."""
    w = usb_wcb(bench)
    m = None
    try:
        m, took = _reboot_while(w, lambda: w.send("?VERSION"), CAP_S + 6)
        held = next((x for x in _window(w.dev, m) if x.startswith("Restart held off")), None)
        bench.note(f"W1 restarted {took if took is None else round(took, 1)} s after ?reboot under a command a "
                   f"second; {held or 'no cap line'}")
        assert took is not None, f"W1 did not restart within {CAP_S + 6:.0f} s of ?reboot under a command a second"
        assert CAP_S - 1.5 <= took <= CAP_S + 3.0, f"W1 restarted {took:.1f} s after ?reboot, not at the {CAP_S:.0f} s cap"
        assert held and re.match(r"^Restart held off \d+ s by commands that kept arriving - restarting anyway$",
                                 held.rstrip()), f"no ungated 'Restart held off' line before the restart: {held!r}"
    finally:
        if m is not None:
            _finish_reboot(bench, w, m)


_CHAR = {}


def _char(bench):
    """Run ?ETM,CHAR once per bench session -> {rec: recommended ms, rows: [(phase, wcb, avg, max, missed%)], and what
    the run shows the other tests: again (W1's reply to a second ?ETM,CHAR 1 s in), restored (W1 turned ?DEBUG,ETM
    back on at the end), w2 (W2's console lines, None without its USB), w2_mark, ports ({wire: bytes}) }. ETM debug is
    on at the start so the restore can be seen. It measures only the peers W1 sees online, so it waits for them first
    (_peers_online), and an abort fails at once instead of running out the 180 s."""
    if id(bench) not in _CHAR:
        w = usb_wcb(bench)
        waited = _peers_online(bench, w)
        w2 = WCB(bench.dev("wcb2")) if bench.usb_wcbs().get(2) else None
        ports = [l for l in (bench.links.get(n, s) for n in (1, 2) for s in ("S2", "S3", "S4", "S5")) if l]
        w.run("?DEBUG,ETM,ON")
        try:
            watch = Watch(*ports)
            m2 = w2.dev.mark() if w2 else None
            m = w.send("?ETM,CHAR")
            w.dev.expect(r"^Phase 1 - Individual Baseline", timeout=5, since=m)
            time.sleep(1.0)
            ma = w.send("?ETM,CHAR")               # a second start mid-run (re-scan #17)
            time.sleep(1.0)
            again = [x.rstrip() for x in w.dev.since(ma)]
            rec = w.dev.expect(r"Recommended ETM timeout: (\d+)ms|\[ETM\] Characterization aborted: (.*)",
                               timeout=180, since=m)
            assert rec.group(1), f"?ETM,CHAR aborted: {rec.group(2).strip()}"
            time.sleep(2)
            restored = any(x.startswith("[ETM CHAR] ETM debug re-enabled.") for x in w.dev.since(m))
            if w2:
                try:                                   # a peer's load runs 10 s, longer than the whole run at COUNT 10
                    w2.dev.expect(r"^ETM load test complete: ", timeout=14, since=m2)
                except AssertionError:
                    pass                               # etm.char_load_on_peers reports it
            w2_lines = [x.rstrip() for x in w2.dev.since(m2)] if w2 else None
            port_bytes = {l.key: watch.got(l) for l in ports}
        finally:
            w.run("?DEBUG,ETM,OFF")
        rows, phase = [], None
        for t in w.dev.since(m):
            p = re.match(r"^ Phase (\d) - ", t)     # results block; progress lines have no leading space
            if p:
                phase = int(p.group(1))
            r = re.search(r"WCB(\d+): Min: \d+ms, Max: (\d+)ms, Avg: (\d+)ms, Missed: (\d+)%", t)
            if r and phase:
                rows.append((phase, int(r.group(1)), int(r.group(3)), int(r.group(2)), int(r.group(4))))
        bench.note(f"peers online after {waited:.1f} s; ETM CHAR recommended {rec.group(1)} ms; " +
                   "; ".join(f"P{p} W{b} avg {a} max {mx} missed {ms}%" for p, b, a, mx, ms in rows))
        _CHAR[id(bench)] = {"rec": int(rec.group(1)), "rows": rows, "again": again, "restored": restored,
                            "w2": w2_lines, "w2_dev": w2.dev if w2 else None, "w2_mark": m2, "ports": port_bytes}
    return _CHAR[id(bench)]


@test("etm.char_unicast", "?ETM,CHAR phases 1-2 (unicast, no load) lose under 5% to every peer", needs=["wcb1"])
def char_unicast(bench):
    rows = _char(bench)["rows"]
    rows = [r for r in rows if r[0] in (1, 2)]
    assert rows, "no phase 1/2 rows in ?ETM,CHAR output"
    lossy = [f"P{p} W{b} missed {ms}%" for p, b, _, _, ms in rows if ms > 5]
    assert not lossy, f"lossy unicast links: {lossy}"


@test("etm.char_loaded", "?ETM,CHAR phase 3 (loaded network) actually transmits its broadcasts",
      needs=["wcb1"])
def char_loaded(bench):
    # Phase 3 sends every 3rd message as a broadcast (WCB.ino:1614). A miss rate equal to that
    # share means the broadcasts were never transmitted, not lost: sendESPNowMessage returns
    # early for target 0 while the global lastReceivedViaESPNOW is latched (WCB.ino:2549), and
    # processETMChar runs from loop() outside any command snapshot. See docs/HIL_TESTING.md §5.
    # 4% (1 of 27) or 7% on W3, the peer that gets the last unicast, in every run until WCB ff7eedd: NaviCore ACKs
    # every broadcast unasked, its ACKs counted toward the phase's completion, and the phase ended as soon as its last
    # message went out (run 20261009-182550). W3's console had logged an ACK for every phase 3 message.
    rows = _char(bench)["rows"]
    rows = [r for r in rows if r[0] == 3]
    assert rows, "no phase 3 rows in ?ETM,CHAR output"
    lossy = [f"W{b} missed {ms}%" for _, b, _, _, ms in rows if ms > 5]
    assert not lossy, (f"phase 3 lossy: {lossy} — 30% is exactly the broadcast share; "
                       "broadcasts suppressed by lastReceivedViaESPNOW (WCB.ino:2549)?")


@test("etm.char_load_on_peers", "?ETM,CHAR phase 3 really loads the mesh: W2 runs its 10 s generator (start line, then 'complete: N frame(s) sent' about 10 s later), and no load text reaches a port on W1 or W2 (re-scan #4)", needs=["wcb1", "wcb2"])
def char_load_on_peers(bench):
    """WCB coverage re-scan #4 (docs/hil_plan/WCB.md WCB-WP24 row 1). Phase 3 sends ETMLOAD to every peer under ETM,
    and the peer's ETM receive path ACKed it and did nothing (only the plain path, which an ETM peer never takes,
    started the generator); the generator's own frames were plain, so ETM peers dropped them. Phase 3 "Loaded Network"
    measured an idle mesh. The load is untracked ETM frames starting ETMCHAR_, which receivers ACK and drop unrun."""
    c = _char(bench)
    if c["w2"] is None:
        raise Skip("W2 has no USB console on this bench")
    started = [x for x in c["w2"] if x == "ETM load test started by remote board."]
    done = [re.match(r"^ETM load test complete: (\d+) frame\(s\) sent\.$", x) for x in c["w2"]]
    done = [d for d in done if d]
    assert len(started) == 1, f"W2 printed {len(started)} load start line(s) during W1's ?ETM,CHAR"
    assert len(done) == 1, f"W2 printed {len(done)} load completion line(s)"
    t0 = _at(c["w2_dev"], r"^ETM load test started by remote board\.", c["w2_mark"])
    t1 = _at(c["w2_dev"], r"^ETM load test complete: ", c["w2_mark"])
    frames = int(done[0].group(1))
    bench.note(f"W2's load ran {t1 - t0:.1f} s and sent {frames} frame(s)")
    assert 9.0 <= t1 - t0 <= 13.0, f"W2's load ran {t1 - t0:.1f} s, not about 10 s"
    assert frames >= 50, f"W2's load sent only {frames} frame(s) in 10 s (one per ?ETM,DELAY, 100 ms by default)"
    leaked = {k: v for k, v in c["ports"].items() if any(s in v for s in (b"LOAD", b"ETMCHAR", b"ETMLOAD"))}
    assert not leaked, f"load text reached ports: {leaked}"


@test("etm.char_second_start_refused", "A second ?ETM,CHAR while one runs is refused ('already running'), and the run still turns ?DEBUG,ETM back on at its end (re-scan #17)", needs=["wcb1"])
def char_second_start_refused(bench):
    """WCB coverage re-scan #17 (WCB-WP24 row 4, case d). Neither local entry point checked etmCharRunning, and
    startETMChar saved the ALREADY-suppressed debugETM as the state to restore, so a restarted run ended with
    ?DEBUG,ETM off. The check is in startETMChar now, for every entry point."""
    c = _char(bench)
    assert "ETM characterization is already running." in c["again"], f"the second ?ETM,CHAR printed {c['again']}"
    assert c["restored"], "the run did not print '[ETM CHAR] ETM debug re-enabled.' although ETM debug was on at its start"


@test("etm.char_relay_refusal_reported", "A relayed ?MGMT,ETM,CHAR the target cannot start is answered at once with the reason, and the requester is released: W2's next local run sends W1 nothing (re-scan #18)", needs=["wcb1", "wcb2"])
def char_relay_refusal_reported(bench):
    """WCB coverage re-scan #18 (docs/hil_plan/WCB.md WCB-WP24 row 2, latch arm). handleETMReqPacket latched the
    requester and then called startETMChar, whose early returns (ETM off, WCBQ < 2) neither told the requester nor
    cleared the latch: the Wizard's Network Test waited for nothing, and the target's next LOCAL run sent its results
    to that requester. W2's ?WCBQ goes to 1 for the refusal - live, and W1 stays in W2's floor (ids 1..WCBQ) - and
    back from W2's chain. The request is one unACKed packet, so it is sent a second time if the first gets nothing."""
    w1, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    problems = []
    with config_guard(bench, 2) as before:
        q = token(before[2], "?WCBQ,")
        if not q or int(q.split(",")[1]) < 2:
            raise Skip(f"W2's chain has {q}")
        w1.run("?DEBUG,MGMT,ON")
        try:
            try:
                w2.run("?WCBQ,1")
                reply = None
                for _ in range(2):
                    m = w1.send("?MGMT,ETM,CHAR,2")
                    try:
                        reply = w1.dev.expect(r"^\[MGMT:ETM,2\](.*)", timeout=6, since=m).group(1)
                        break
                    except AssertionError:
                        pass
                if reply is None:
                    problems.append("W1 got no [MGMT:ETM,2] reply to a request W2 could not start")
                elif "ETM characterization could not run on WCB2: fewer than 2 WCBs in the network (?WCBQ)" not in reply:
                    problems.append(f"W2's reply was {reply!r}")
            finally:
                w2.run(q)
            m1 = w1.dev.mark()
            m2 = w2.send("?ETM,CHAR")
            end = w2.dev.expect(r"Recommended ETM timeout: (\d+)ms|\[ETM\] Characterization aborted: (.*)",
                                timeout=180, since=m2)
            time.sleep(3)
            bench.note(f"W2's local run: {end.group(0).strip()}")
            if any(x.startswith("[MGMT:ETM,2]") for x in w1.dev.since(m1)):
                problems.append("W2's later LOCAL ?ETM,CHAR sent its results to W1: the requester stayed latched")
        finally:
            w1.run("?DEBUG,MGMT,OFF")
    assert not problems, "; ".join(problems)


# ============================================================ the sequence wrap (WCB-WP46, opt-in etm_seq_wrap)
SEQ_TOP = 65535
WRAP_MARGIN = 300       # the bulk flood stops this short of the top; the rest is counted with ?DEBUG,ETM on
SENT_SEQ = re.compile(r"^\[ETM\] Sent seq (\d+): (.*)$")


def _seq_now(w):
    """W1's ETM sequence counter: one untracked JSON broadcast with ?DEBUG,ETM on names the number it spent
    (sendESPNowMessage, WCB.ino:3099, printed at :3115) -> that number."""
    tag = nonce()
    w.run("?DEBUG,ETM,ON")
    try:
        out = w.run('{"hs":"%s"}' % tag)
    finally:
        w.run("?DEBUG,ETM,OFF")
    for x in out:
        m = SENT_SEQ.match(x.rstrip())
        if m and tag in m.group(2):
            return int(m.group(1))
    raise AssertionError("W1 printed no '[ETM] Sent seq' line for its JSON broadcast")


def _json_burst(w, count, block=400):
    """`count` untracked JSON broadcasts typed on W1's console, `block` to a line -> (the 'Send failed' lines, the
    'Command queue is full' lines) seen. Each '{}' token is a plain-text broadcast (handleSingleCommand, WCB.ino:5996-
    5999) that sendESPNowMessage sends as an ETM frame it does not track (:3101-3106): exactly one sequence number
    (nextEtmSeq, :703-706), no ACK, no retry, no pending slot. The number is spent before the send, so a full ESP-NOW
    TX queue ('Send failed', :3118) costs one too. The console reader waits for command-queue room (enqueueCommand,
    :2591-2599), and each line's echo paces the next."""
    failed = full = 0
    left = count
    while left > 0:
        n = min(block, left)
        out = w.run("^".join(["{}"] * n), timeout=120)
        failed += sum(1 for x in out if x.startswith("[ETM] Send failed seq"))
        full += sum(1 for x in out if "Command queue is full" in x)
        left -= n
    return failed, full


@test("etm.seq_wrap", "OPT-IN (etm_seq_wrap): W1's ETM sequence counter, driven past 65,535 by about 65,000 untracked JSON broadcasts, skips 0: no '[ETM] Sent seq 0', the unicast sent right after 65,535 goes out as a small non-zero number, and W2 - rebooted first, so its duplicate ring for W1 still holds empty (0) slots - runs it: the marker reaches W2 S2 once and W2 logs no duplicate (re-scan #19; ~7 min, W2 reboots once)", needs=["wcb1", "wcb2"], links=["W2S2"], opt_in="etm_seq_wrap")
def seq_wrap(bench):
    """WCB-WP46 (wcb.etm.seq_wrap_zero). The counter is a uint16_t (WCB.ino:699) that nextEtmSeq pre-increments and
    steps over 0 (:700-706, re-scan #19); a receiver's duplicate ring uses 0 for an empty slot (:737-751, filled at
    :5352-5363), so a command numbered 0 reaching a ring with an empty slot for its sender would be ACKed and never
    run. W2 is rebooted first to empty its ring for W1, and W1 sends W2 nothing tracked until the marker, so the ring
    still has empty slots when W1 wraps (JSON broadcasts are never put in a ring, :5358-5364). The flood is '{}'
    typed on W1's console: W1 prints nothing per frame, W2 and NaviCore consume JSON silently, and W1's port
    broadcasts are off meanwhile (put back after), or each frame would also cost a write to every port. The count is
    read before and after the bulk (_seq_now), and the last stretch runs with ?DEBUG,ETM on to see the numbers around
    the wrap. The flood can starve W1's heartbeats for a while: the test ends by waiting for W1 to see W2 online."""
    far = link(bench, 2, "S2")
    w, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    require_tokens(bench, 1, "?ETM,ON", "?BCAST,OUT,S0,OFF")
    require_tokens(bench, 2, "?ETM,ON")
    bad, facts = [], {}
    with config_guard(bench, 1) as before:
        ports = [p for p in ("S2", "S3", "S4", "S5") if f"?BCAST,OUT,{p},ON" in before[1]]
        start = w.dev.mark()
        try:
            for p in ports:
                w.run(f"?BCAST,OUT,{p},OFF")
            w2.reboot()                                    # W2's duplicate ring for W1 starts empty
            _peers_online(bench, w)
            facts["heap_before"] = _heap_note(bench, "etm.seq_wrap", "before the flood")
            c0 = _seq_now(w)
            t0 = time.monotonic()
            bulk = SEQ_TOP - c0 - WRAP_MARGIN
            if bulk > 0:
                facts["send_failed"], facts["queue_full"] = _json_burst(w, bulk)
            facts["flood_s"] = round(time.monotonic() - t0)
            c1 = _seq_now(w)
            facts["counter"] = (c0, c1)
            if c1 < c0 or c1 > SEQ_TOP - 2:
                raise AssertionError(f"W1's counter went from {c0} to {c1} over a flood of {max(bulk, 0)}: the last "
                                     f"stretch before the wrap could not be counted (did W1 reboot, or wrap early?)")
            w.run("?DEBUG,ETM,ON")
            w2.run("?DEBUG,ETM,ON")
            wm, m2 = w.dev.mark(), w2.dev.mark()
            _json_burst(w, SEQ_TOP - c1)
            t = marker("W")
            watch = Watch(far)
            w.run(f";W2,;S2{t}")
            try:
                watch.expect(far, t.encode() + b"\r", timeout=5)
            except AssertionError:
                pass
            time.sleep(1.5)
            lines, lines2 = [x.rstrip() for x in w.dev.since(wm)], [x.rstrip() for x in w2.dev.since(m2)]
            seqs = [(int(m.group(1)), m.group(2)) for m in map(SENT_SEQ.match, lines) if m]
            mine = next((s for s, text in seqs if text.startswith(f";S2{t}")), None)
            top = max((s for s, text in seqs if text == "{}"), default=None)
            facts.update(marker_seq=mine, top_json_seq=top)
            if any(s == 0 for s, _ in seqs):
                bad.append("W1 sent a command numbered 0")
            if mine is None:
                bad.append("W1 printed no '[ETM] Sent seq' line for the marker unicast")
            elif not 1 <= mine <= 20:
                bad.append(f"the unicast after the wrap went out as seq {mine}, expected a small number above 0")
            if top is None or top < SEQ_TOP - 5:
                bad.append(f"the last JSON broadcasts before the marker reached seq {top}, not the top ({SEQ_TOP}): the "
                           f"wrap was not where the test looked")
            if mine is not None and not any(re.match(rf"^\[ETM\] Received seq {mine} from WCB1: ;S2{t}", x) for x in lines2):
                bad.append(f"W2 logged no '[ETM] Received seq {mine} from WCB1' for the marker")
            dup = [x for x in lines2 if "Duplicate seq" in x and "from WCB1" in x]
            if dup:
                bad.append(f"W2 dropped W1's commands as duplicates: {dup[:2]}")
            copies = watch.got(far).count(t.encode() + b"\r")
            if copies != 1:
                bad.append(f"W2 S2 got the marker {copies} time(s), expected once")
            if w.rebooted_since(start):
                bad.append("W1 rebooted during the test (brownout under the flood?)")
        finally:
            w.run("?DEBUG,ETM,OFF")
            try:
                w2.run("?DEBUG,ETM,OFF")
            except AssertionError:
                pass
            for p in ports:
                w.run(f"?BCAST,OUT,{p},ON")
            time.sleep(12.0)                               # a heartbeat period: every board hears W1 again
            _peers_online(bench, w, strict=False)
            try:
                _heap_note(bench, "etm.seq_wrap", "after the flood")
            except AssertionError as e:
                bench.note(f"etm.seq_wrap: the heap after the flood could not be read: {e}")
    bench.note(f"etm.seq_wrap: {facts}")
    assert not bad, "; ".join(bad)


# ============================================================ a full command queue (tracker #108)
FAILED_TO_ACK = re.compile(r"^\[ETM\] WCB1 failed to ACK seq (\d+) after 3 retries: (.*)$")
CANCELED = re.compile(r"^\[ETM\] WCB1 offline, canceling retry for seq (\d+)")
REFUSED = re.compile(r"^\[ETM\] (\d+) command\(s\) refused unacknowledged")


@test("etm.full_queue_refused_not_lost", "An ETM command that finds W1's command queue full is refused unacknowledged, "
      "never ACKed and then discarded: with the queue held full by a console flood, each ;W1,;S2<marker> W2 sends "
      "either reaches W1 S2 or W2 reports it failed; ?STATS then shows 'ETM: N command(s) refused unacknowledged' with "
      "the N loop()'s refusal lines added up to, and ?STATS,RESET clears it with no stray refusal line after "
      "(tracker #108; ~40 s)", needs=["wcb1", "wcb2"], links=["W1S2"])
def full_queue_refused_not_lost(bench):
    """Tracker #108. espNowReceiveCallback sent the ETM ACK (etmSendAck) before it parsed and queued the command, and a
    WiFi-task enqueue never waits (CLAUDE.md rule 11), so a command that found W1's 200-slot queue full was discarded
    after its sender was told it arrived: seven such during etm.seq_wrap's flood, each 'Command queue is full!
    Discarding command.' The fix refuses it unacknowledged when the queue cannot take its tokens
    (commandQueueCanTake), counts it, and loop() says '[ETM] N command(s) refused unacknowledged'; the sender retries
    and, if every retry meets a full queue, prints '[ETM] WCB1 failed to ACK seq N after 3 retries: <cmd>'. So every
    marker W2 sent (its '[ETM] Sent seq N' line, ?DEBUG,ETM) must reach W1 S2 or be named in a failure on W2, or be
    in a retry W2 cancelled for W1 going offline. The flood is etm.seq_wrap's: '{}' tokens typed on W1's console, which
    the console reader keeps the queue topped up with while loop() drains it at the radio's pace (tracker #102's
    pacing); W1's port broadcasts are off meanwhile. It skips when no refusal and no discard showed that the queue was
    ever full while the markers arrived.

    Coverage re-scan 2026-10-12, etm.full_queue_refused_unacked: buildStatsString prints etmQueueFullRefused as 'ETM: N
    command(s) refused unacknowledged - the command queue was full' when it is not zero, and resetESPNowStats zeroes
    it; reportEtmQueueRefusals re-syncs its own count when it finds the counter below it, so the reset is not reported
    as a refusal (with the counter unsigned, a missed re-sync would print a four-billion count). ?STATS,RESET runs
    before the flood, so the ?STATS count and the sum of loop()'s lines since then must agree."""
    near = link(bench, 1, "S2")
    w, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    require_tokens(bench, 1, "?ETM,ON")
    require_tokens(bench, 2, "?ETM,ON")
    tags = [marker(f"Q{k:02d}") for k in range(24)]
    facts = {"send_failed": 0, "queue_full": 0}
    stop = threading.Event()
    flood_err = []

    def flood():
        left = 4000
        try:
            while left > 0 and not stop.is_set():
                n = min(400, left)
                out = w.run("^".join(["{}"] * n), timeout=120)
                facts["send_failed"] += sum(1 for x in out if x.startswith("[ETM] Send failed seq"))
                facts["queue_full"] += sum(1 for x in out if "Command queue is full" in x)
                left -= n
        except Exception as e:                    # reported below; the main thread still puts W1 back
            flood_err.append(e)

    with config_guard(bench, 1) as before:
        ports = [p for p in ("S2", "S3", "S4", "S5") if f"?BCAST,OUT,{p},ON" in before[1]]
        th = None
        try:
            for p in ports:
                w.run(f"?BCAST,OUT,{p},OFF")
            w2.run("?DEBUG,ETM,ON")
            w.run("?STATS,RESET")
            watch = Watch(near)
            m1, m2 = w.dev.mark(), w2.dev.mark()
            th = threading.Thread(target=flood, daemon=True)
            th.start()
            time.sleep(3.0)                       # the console reader has the queue full by now
            for t in tags:
                if not th.is_alive():
                    break
                w2.send(f";W1,;S2{t}")
                time.sleep(0.3)
            th.join(timeout=180)
            time.sleep(4.0)                       # three retries at the ETM timeout, then loop()'s report line
            got = watch.got(near)
            lines1 = [x.rstrip() for x in w.dev.since(m1)]
            lines2 = [x.rstrip() for x in w2.dev.since(m2)]
            stats = [x.rstrip() for x in w.run("?STATS")]
            time.sleep(1.5)                       # loop() reports at most once a second: let the last line out
            reported = sum(int(m.group(1)) for m in map(REFUSED.match, w.dev.since(m1)) if m)
            rm = w.dev.mark()
            w.run("?STATS,RESET")
            after_reset = [x.rstrip() for x in w.run("?STATS")]
            time.sleep(1.5)
            stray = [x.rstrip() for x in w.dev.since(rm) if REFUSED.match(x)]
        finally:
            stop.set()
            if th is not None:
                th.join(timeout=150)              # never type on W1's console alongside the flood
            try:
                w2.run("?DEBUG,ETM,OFF")
            except AssertionError:
                pass
            for p in ports:
                w.run(f"?BCAST,OUT,{p},ON")
    if flood_err:
        raise AssertionError(f"W1's console flood failed: {flood_err[0]}")
    sent = {}                                     # seq -> marker, for the markers W2 really sent
    for x in lines2:
        m = SENT_SEQ.match(x)
        if m and m.group(2).startswith(";S2"):
            sent[int(m.group(1))] = m.group(2)[3:]
    mine = {seq: t for seq, t in sent.items() if t in tags}
    arrived = {t for t in mine.values() if (t.encode() + b"\r") in got}
    failed = {m.group(2)[3:] for m in map(FAILED_TO_ACK.match, lines2) if m and m.group(2).startswith(";S2")}
    canceled = {mine[int(m.group(1))] for m in map(CANCELED.match, lines2) if m and int(m.group(1)) in mine}
    refused = sum(int(m.group(1)) for m in map(REFUSED.match, lines1) if m)
    discarded = sum(1 for x in lines1 if "Command queue is full" in x) + facts["queue_full"]
    lost = sorted(t for t in mine.values() if t not in arrived | failed | canceled)
    bench.note(f"etm.full_queue_refused_not_lost: {len(mine)} markers sent, {len(arrived)} arrived, {len(failed)} "
               f"reported failed, {len(canceled)} cancelled offline; W1 refused {refused}, discarded {discarded}; "
               f"flood {facts}")
    stat_lines = [x for x in stats if "refused unacknowledged" in x]
    want = [f"ETM: {reported} command(s) refused unacknowledged - the command queue was full"] if reported else []
    if stat_lines != want:
        raise AssertionError(f"?STATS refusal line {stat_lines} does not match the {reported} refusal(s) loop() reported")
    if any("refused unacknowledged" in x for x in after_reset):
        raise AssertionError(f"?STATS,RESET left the refusal line: {[x for x in after_reset if 'refused' in x]}")
    if stray:
        raise AssertionError(f"after ?STATS,RESET loop() still reported refusals: {stray}")
    if not mine:
        raise AssertionError("W2 printed no '[ETM] Sent seq' line for any marker (is ?DEBUG,ETM on, is W1 a peer?)")
    if lost:
        raise AssertionError(f"(tracker #108) {len(lost)} of {len(mine)} markers W2 sent neither reached W1 S2 nor "
                             f"were reported failed on W2 - ACKed, then discarded for a full queue ({discarded} "
                             f"discard line(s) on W1): {lost[:6]}")
    if not refused and not discarded:
        raise Skip(f"W1's queue never refused or discarded a command while the markers arrived ({len(arrived)} of "
                   f"{len(mine)} ran): nothing was tested")


# ============================================================ ?ETM,CHAR through a WCB relay, and its guards (WCB-WP24)
REC_TIMEOUT = re.compile(r"Recommended ETM timeout: (\d+)ms")


@test("etm.char_relay_roundtrip", "The Wizard's per-board Network Test: ?MGMT,ETM,CHAR,2 on W1 has W2 run the characterisation with W1 latched as the requester, and W1 prints W2's results as [MGMT:ETM,2] - the same recommended timeout W2 printed itself (one more request if the single-pass reply is lost, tracker #109)", needs=["wcb1", "wcb2"])
def char_relay_roundtrip(bench):
    """WCB-WP24 row 2 (wcb.mgmt.etm_char_relay_e2e, wcb.etm.char_relay_roundtrip). handleMgmtForward sends ETM_REQ;
    handleETMReqPacket (WCB.ino) latches the requester and starts the run; the results go back as ETM_FRAG broadcasts
    (sendResultFrags, once, unacknowledged: tracker #109) and handleETMFragPacket queues them for loop(), which prints
    '[MGMT:ETM,2]' and the text - which starts with a newline, so the tag stands alone on its line and the block follows.
    W2's own console prints the same block. The reply leaves while the 10 s load its run started is still on the air,
    so one lost reply is asked for again once W2 has finished."""
    own = bench.usb_wcbs().get(2)
    if not own:
        raise Skip("W2 has no USB console here: there is nothing to compare the relayed result with")
    w, w2 = usb_wcb(bench), WCB(bench.dev(own))
    require_tokens(bench, 1, "?ETM,ON")
    require_tokens(bench, 2, "?ETM,ON")
    _peers_online(bench, w)
    notes, got, want = [], None, None
    for attempt in (1, 2):
        m1, m2 = w.dev.mark(), w2.dev.mark()
        w.send("?MGMT,ETM,CHAR,2")
        try:
            w2.dev.expect(REC_TIMEOUT.pattern, timeout=90, since=m2)
        except AssertionError:
            raise AssertionError(f"try {attempt}: W2 never finished the characterisation it was asked for") from None
        want = REC_TIMEOUT.search(" ".join(w2.dev.since(m2))).group(1)
        try:
            w.dev.expect(r"^\[MGMT:ETM,2\]", timeout=8, since=m1)
        except AssertionError:
            notes.append(f"try {attempt}: no [MGMT:ETM,2] on W1 within 8 s of W2's result (a lost single-pass reply)")
            time.sleep(12.0)                          # past the 10 s load W2's run started, before asking again
            continue
        time.sleep(1.0)                               # the block after the tag line
        lines = [x.rstrip() for x in w.dev.since(m1)]
        i = next(k for k, x in enumerate(lines) if x.startswith("[MGMT:ETM,2]"))
        rec = REC_TIMEOUT.search(" ".join(lines[i:]))
        got = rec.group(1) if rec else None
        break
    bench.note("etm.char_relay_roundtrip: " + ("; ".join(notes) + "; " if notes else "") +
               f"W2 recommended {want} ms, W1's relayed block {got}")
    assert got is not None, "W1 printed no relayed result with a recommended timeout: " + "; ".join(notes)
    assert got == want, f"W1's relayed block recommends {got} ms, W2 printed {want} ms"


@test("etm.char_guard_wcbq", "A local ?ETM,CHAR refuses to run on a board whose mesh floor (?WCBQ) is under 2 - 'ETM Char requires at least 2 WCBs in the network (?WCBQ).' - and runs nothing (W1 at ?WCBQ,1 for a moment, then put back)", needs=["wcb1"])
def char_guard_wcbq(bench):
    """WCB-WP24 row 4, the local WCBQ half (wcb.etm.char_guards_abort). startETMChar (WCB.ino) refuses with WCBQ < 2
    before anything is sent; the relayed refusal is etm.char_relay_refusal_reported. ?WCBQ applies live (peer
    registrations reconciled, no reboot), so W1 is put back at once, and config_guard fails the test if it was not."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        q = token(before[1], "?WCBQ,")
        if not q or int(q.split(",")[1]) < 2:
            raise Skip(f"W1's mesh floor is {q!r}: the guard is what a normal run would hit")
        try:
            w.run("?WCBQ,1")
            out = w.run("?ETM,CHAR")
            if not any("ETM Char requires at least 2 WCBs in the network (?WCBQ)." in x for x in out):
                problems.append(f"?ETM,CHAR at ?WCBQ,1 printed {[x for x in out if x.strip()][:3]}")
            if any(x.startswith("Phase 1") for x in out):
                problems.append("?ETM,CHAR started a phase at ?WCBQ,1")
        finally:
            w.run(q)
    assert not problems, "; ".join(problems)


CLAMP = re.compile(r"^\[ETM CHAR\] (\d+) online peers x (\d+) messages exceeds the 200-message phase cap .* sampling "
                   r"(\d+) per board instead\.")


@test("etm.char_per_board_clamp", "?ETM,COUNT,200 with two or more online peers is clamped per board so a phase never passes its 200-message row: one '[ETM CHAR] N online peers x 200 messages exceeds the 200-message phase cap - sampling M per board' notice, and every phase still completes; once the load is over every WCB transmits again - its unicast reaches W1 and it gives no frame up for a heartbeat period (~1.5 min, probe2 joins as a temporary client)", needs=["wcb1", "wcb2", "probe2"])
def char_per_board_clamp(bench):
    """WCB-WP24 row 3 (wcb.etm.char_per_board_clamp). processETMChar (WCB.ino) clamps messages per board to
    ETM_CHAR_MAX_MSGS / peerCount, the row length of etmCharPhaseSentTimes[3][200]; unclamped, phases 1-2 clobbered each
    other's timestamps and phase 3 wrote past the array, and every phase ran to its full timeout. The one-shot notice
    (etmCharClampWarned) prints once per run. probe2 joins as a temporary client so W1 counts at least two online
    peers whatever else is on the mesh; W1's ?ETM,COUNT is put back in a finally, and config_guard fails the test if it
    was not."""
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?ETM,ON")
    tx_problems, tx_facts = [], {}
    with config_guard(bench, 1) as before:
        count = token(before[1], "?ETM,COUNT,")
        with probe_in_mesh(bench, "probe2", 15):
            _peers_online(bench, w)
            _heap_note(bench, "etm.char_per_board_clamp", "before the load")
            try:
                w.run("?ETM,COUNT,200")
                since = {n: WCB(bench.dev(dev)).dev.mark() for n, dev in bench.usb_wcbs().items()
                         if n != bench.usb_wcb_number()}
                m = w.send("?ETM,CHAR")
                rec = w.dev.expect(r"Recommended ETM timeout: (\d+)ms|\[ETM\] Characterization aborted: (.*)",
                                   timeout=240, since=m)
                time.sleep(1.0)
                lines = [x.rstrip() for x in w.dev.since(m)]
                _tx_after_load(bench, w, since, tx_problems, tx_facts)
                _heap_note(bench, "etm.char_per_board_clamp", "after the load")
            finally:
                w.run(count or "?ETM,COUNT,20")
    notices = [c for c in map(CLAMP.match, lines) if c]
    phases = {int(p.group(1)) for p in (re.match(r"^ Phase (\d) - ", x) for x in lines) if p}
    bench.note(f"etm.char_per_board_clamp: {len(notices)} notice(s) "
               f"({notices[0].group(0)[:110] if notices else '-'}); result phases {sorted(phases)}; "
               f"recommended {rec.group(1)} ms" if rec.group(1) else f"aborted: {rec.group(2)}")
    assert rec.group(1), f"?ETM,CHAR aborted: {rec.group(2).strip()}"
    assert len(notices) == 1, f"{len(notices)} clamp notice(s), expected exactly one"
    peers, per = int(notices[0].group(1)), int(notices[0].group(3))
    assert peers >= 2 and per == 200 // peers, f"the notice says {peers} peers sampled at {per} each"
    assert phases == {1, 2, 3}, f"the results block names phases {sorted(phases)}, not all three"
    bench.note(f"etm.char_per_board_clamp: after the load {tx_facts}")
    assert not tx_problems, "; ".join(tx_problems)


# ============================================================ F18: the WiFi task's came-ONLINE line, queued for loop()
@test("etm.came_online_outside_backup", "A peer's '[ETM] WCBn came ONLINE' line is queued for loop() (F18): while W2 reboots, none of W1's back-to-back ?backup outputs has one inside it, both chains of each pass their CRC, and W1 prints W2's boot announces as whole lines", needs=["wcb1", "wcb2"])
def came_online_outside_backup(bench):
    """espNowReceiveCallback runs on the WiFi task and used to print the line there, where it landed between the two
    writes of a Serial.println in loop() - a ?backup section header and its chain (wizard.remote_pull, run
    20260924-234056) - and inside a line on the WebSocket and RTERM tees. It now goes through statusQueueOut and is
    printed by drainStatusOut beside drainMgmtOut (WCB.ino), between commands, so a whole ?backup runs in one loop()
    pass with none inside it. W2's reboot sends three boot announces about 1.2 s apart (a line each on W1, marked
    '(boot)'); W1 prints ?backup the whole time. A came-ONLINE line without '(boot)' is whole too: W1 prints one when it
    had W2 marked offline and hears it again (WCB.ino boardMarkSeen), as in run 20261006-122850, where W2's mesh
    transmit had stalled during the test before (etm.char_per_board_clamp) and W1 had called it offline; it is noted.
    Only a line that is neither form - cut, or run into another - fails as not whole."""
    from suites.s03_wcb import CHK_LINE, backup_chain_lines
    from hil.wcb import chain_crc
    own = bench.usb_wcbs().get(2)
    if not own:
        raise Skip("W2 has no USB console here to restart it from")
    w, w2 = usb_wcb(bench), WCB(bench.dev(own))
    boot = re.compile(r"^\[ETM\] WCB2 came ONLINE \(boot\) \(src MAC: [0-9A-F]{2}(:[0-9A-F]{2}){5}\)$")
    any_form = re.compile(r"^\[ETM\] WCB2 came ONLINE (?:\(boot\) )?\(src MAC: [0-9A-F]{2}(:[0-9A-F]{2}){5}\)$")
    problems, inside, backups, broken = [], 0, 0, 0
    m2 = w2.dev.mark()
    w2.dev.send("?reboot")
    w2.dev.expect(r"^Rebooting now", timeout=WCB.REBOOT_DEFER_S, since=m2)
    m1 = w.dev.mark()
    end = time.monotonic() + 12.0
    while time.monotonic() < end:
        lines = [x.rstrip() for x in w.run("?backup", timeout=10)]
        backups += 1
        a = next((i for i, x in enumerate(lines) if "WCB Configuration Backup" in x), None)
        b = next((i for i, x in enumerate(lines) if "End of Backup" in x), None)
        if a is None or b is None:
            problems.append(f"backup {backups} is not whole")
            continue
        inside += sum(1 for x in lines[a:b] if x.startswith("[ETM]") and "came ONLINE" in x)
        chains = backup_chain_lines(lines)
        for name in ("configured", "factory"):
            c = CHK_LINE.match(chains.get(name) or "")
            if not c or chain_crc(c.group(1)) != c.group(2).upper():
                broken += 1
        if sum(1 for x in w.dev.since(m1) if boot.match(x.strip())) >= 3:
            break
    seen = [x.strip() for x in w.dev.since(m1) if "came ONLINE" in x]
    whole = [x for x in seen if boot.match(x)]
    plain = [x for x in seen if any_form.match(x) and not boot.match(x)]
    w2.wait_boot(m2)
    bench.note(f"etm.came_online_outside_backup: {backups} backups on W1, {len(seen)} came-ONLINE line(s) "
               f"({len(whole)} whole boot lines, {len(plain)} without '(boot)': W1 had W2 marked offline), {inside} "
               f"inside a backup, {broken} broken chain(s)")
    if not whole:
        problems.append("W1 printed none of W2's boot announces as a whole '[ETM] WCB2 came ONLINE (boot)' line")
    if inside:
        problems.append(f"{inside} came-ONLINE line(s) printed inside a ?backup output")
    if broken:
        problems.append(f"{broken} chain(s) failed their CRC")
    cut = [x for x in seen if not any_form.match(x)]
    if cut:
        problems.append(f"{len(cut)} came-ONLINE line(s) not whole: {[x[:60] for x in cut][:2]}")
    assert not problems, "; ".join(problems)


# ============================================================ coverage re-scan 2026-10-12 (low-risk rows)
@test("etm.char_no_online_peers", "?ETM,CHAR on a board that sees no peer online aborts with '[ETM] Characterization aborted: no online peers.' and turns ?DEBUG,ETM back on (W1 deafened, with HB 4 / MISS 1, until ?STATS shows no peer online; ~25 s)", needs=["wcb1"], links=[])
def char_no_online_peers(bench):
    """Coverage re-scan 2026-10-12, etm.char_run. startETMChar (WCB.ino) saves ?DEBUG,ETM and turns it off for the run;
    processETMChar aborts on its first pass when no active peer is online, and that early return must undo the save
    itself, since printETMCharResults - which normally restores it - never runs. '?MAC,3,<other>' deafens W1 at once
    (s18's _deaf_w1) and HB 4 / MISS 1 has it mark its peers offline 5 s after their last packet. The restored debug
    shows in W1's next '[ETM] Heartbeat sent (WCB1)', due within the 4 s heartbeat. The latched-requester arm needs
    the target to hear a request while it hears no peer, and the >5% loss footer a lossy run: neither is made here."""
    from suites.s18_etm_config_wdp import _deaf_w1, _etm, _restore_etm
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?ETM,ON")
    bad = []
    with config_guard(bench, 1) as before:
        orig = _etm(before[1])
        lines = []
        try:
            w.run("?DEBUG,ETM,ON")
            w.run("?ETM,HB,4")
            w.run("?ETM,MISS,1")
            with _deaf_w1(w, before[1]):
                deadline = time.monotonic() + 20      # a peer W1 already counts offline prints no OFFLINE line
                while any(v.get("online") for v in w.etm_board_stats().values()):
                    if time.monotonic() > deadline:
                        raise AssertionError(f"setup: W1 still sees peers online 20 s after going deaf: "
                                             f"{[n for n, v in w.etm_board_stats().items() if v.get('online')]}")
                    time.sleep(1.0)
                cm = w.dev.mark()
                w.send("?ETM,CHAR")
                try:
                    w.dev.expect(r"^\[ETM\] Characterization aborted: no online peers\.$", timeout=5, since=cm)
                    w.dev.expect(r"^\[ETM\] Heartbeat sent \(WCB1\)$", timeout=7, since=cm)
                except AssertionError:
                    pass
                lines = [x.rstrip() for x in w.dev.since(cm)]
        finally:
            _restore_etm(w, orig, ("HB", "MISS"))
            w.run("?DEBUG,ETM,OFF")
            _peers_online(bench, w, strict=False)
    i_off = next((i for i, x in enumerate(lines) if x.startswith("[ETM CHAR] Temporarily disabling ETM debug")), None)
    i_abort = next((i for i, x in enumerate(lines) if x == "[ETM] Characterization aborted: no online peers."), None)
    if i_off is None:
        bad.append("?ETM,CHAR did not save and turn off ?DEBUG,ETM (no 'Temporarily disabling' line)")
    if i_abort is None:
        bad.append(f"no abort line: {[x for x in lines if 'ETM' in x][:6]}")
    elif not any(x == "[ETM] Heartbeat sent (WCB1)" for x in lines[i_abort + 1:]):
        bad.append("?DEBUG,ETM did not come back on after the abort: no 'Heartbeat sent' line within 7 s")
    if any(x.startswith("Phase 2") for x in lines):
        bad.append("the run went on to phase 2 with no peer online")
    assert not bad, "; ".join(bad)


@test("etm.char_relay_ignored_while_running", "A relayed ?MGMT,ETM,CHAR,2 that reaches W2 while its own local ?ETM,CHAR runs is ignored: W2 logs '[MGMT] ETM char already running — ignoring relay request', W1 gets no [MGMT:ETM,2] - no refusal and, when W2's run ends, none of its results (~60 s)", needs=["wcb1", "wcb2"], links=[])
def char_relay_ignored_while_running(bench):
    """Coverage re-scan 2026-10-12, etm.char_relay. handleETMReqPacket (WCB.ino) returns before latching the requester
    while etmCharRunning: the run in progress keeps its own (no) requester, so W2's results are not sent to W1, and W1
    is not told anything either. Without that check startETMChar's 'already running' refusal would be relayed back as a
    [MGMT:ETM,2] block, and a latched requester would get the local run's results (etm.char_relay_roundtrip is the
    relayed run, etm.char_relay_refusal_reported the refusal of one that cannot start). The request goes 1 s into W2's
    run; W1 is watched until 10 s after W2 prints its recommended timeout."""
    own = bench.usb_wcbs().get(2)
    if not own:
        raise Skip("W2 has no USB console here")
    w, w2 = usb_wcb(bench), WCB(bench.dev(own))
    require_tokens(bench, 1, "?ETM,ON")
    require_tokens(bench, 2, "?ETM,ON")
    _peers_online(bench, w)
    bad = []
    try:
        w2.run("?DEBUG,MGMT,ON")
        m1, m2 = w.dev.mark(), w2.dev.mark()
        w2.send("?ETM,CHAR")
        w2.dev.expect(r"^Phase 1 - Individual Baseline", timeout=5, since=m2)
        time.sleep(1.0)
        w.send("?MGMT,ETM,CHAR,2")
        try:
            w2.dev.expect(r"^\[MGMT\] ETM char already running . ignoring relay request$", timeout=5, since=m2)
        except AssertionError:
            bad.append("W2 printed no 'ETM char already running - ignoring relay request' line")
        try:
            w2.dev.expect(REC_TIMEOUT.pattern, timeout=90, since=m2)
        except AssertionError:
            raise AssertionError("W2's own characterisation never finished") from None
        time.sleep(10.0)
        relayed = [x.rstrip() for x in w.dev.since(m1) if x.startswith("[MGMT:ETM,2]")]
        if relayed:
            bad.append(f"W1 got {len(relayed)} [MGMT:ETM,2] block(s) for a request W2 ignored: {relayed[:2]}")
        lines2 = [x.rstrip() for x in w2.dev.since(m2)]
        if not _has_line(lines2, r"^\[MGMT\] ETM char request from WCB1$"):
            bad.append("W2 never logged W1's request (lost on the air?)")
        if _has_line(lines2, r"could not run"):
            bad.append("W2 refused the relayed request instead of ignoring it")
    finally:
        w2.run("?DEBUG,MGMT,OFF")
    assert not bad, "; ".join(bad)


def _has_line(lines, pattern):
    rx = re.compile(pattern)
    return any(rx.search(x) for x in lines)


# ============================================================ #112 stress: mirrored console under ?backup load
STALL_LINE = "[MESH] ESP-NOW transmit stalled"
STRESS_S = 600


def _radio_sample(w):
    """(free, largest block, min free since boot, frames not sent) from W1's ?STATS: byte heap and wcbEspNowDropped."""
    out = [x.rstrip() for x in w.run("?STATS", timeout=15)]
    heap = next((re.match(r"^Heap: (\d+) free, largest block (\d+), min free since boot (\d+)", x) for x in out
                 if x.startswith("Heap: ")), None)
    drop = next((re.match(r"^ESP-NOW: (\d+) frame\(s\) not sent", x) for x in out if x.startswith("ESP-NOW: ")), None)
    if heap is None:
        raise AssertionError("W1's ?STATS printed no Heap line")
    return (int(heap.group(1)), int(heap.group(2)), int(heap.group(3)), int(drop.group(1)) if drop else 0)


@test("etm.rterm_backup_stress", "OPT-IN (etm_stress): tracker #112's load for 10 minutes - W1 mirrors its console to NaviCore (?RTERM,START,20, re-armed every 10 s) while it prints a ?backup every 5 s and sends W2 a unicast every 3 s: W1 never reports '[MESH] ESP-NOW transmit stalled', a unicast still reaches W2 at the end, and W1's free heap does not drain (its last sample at most 4 KB under its first, never under 10 KB); the heap trend, the radio's not-sent count and the unicasts delivered go to the run notes", needs=["wcb1", "wcb2", "navicore"], links=["W2S2"], opt_in="etm_stress")
def rterm_backup_stress(bench):
    """Tracker #112: on 2026-10-12 W1's ESP-NOW transmit wedged about 3 minutes after boot while it mirrored every
    console line to NaviCore over remote-terminal sessions and printed big ?backup outputs, and stayed wedged for two
    hours (8 frames in flight, thousands of 'radio's queue stayed full', the byte heap down to 5.5 KB) until a reboot.
    This reproduces that load for a bounded time. Each mirrored line is an ESP-NOW unicast to the relay
    (WCBSerial::_flushLine -> wcbEspNowSend, WCB_RemoteTerm.cpp), so a ?backup puts its whole output on the air; the
    session expires 30 s after its last re-arm (RTERM_IDLE_MS), hence the re-arm every 10 s. wcbEspNowReportDrops
    (WCB_EspNow.cpp) prints the stall line from loop() when nothing completes for ESPNOW_STALL_REPORT_MS while frames
    are given up. The test stops early on a stall, stops the mirror, samples W1, and reboots W1 so the bench goes on;
    the firmware is left as it is (no self-recovery): the result is the finding."""
    w = usb_wcb(bench)
    w2s2 = link(bench, 2, "S2")
    samples, uni, problems = [], [], []
    stalled, rebooted = None, False
    with config_guard(bench, 1, 2):
        s0 = _radio_sample(w)
        samples.append((0, s0))
        m0, um = w.dev.mark(), w2s2.mark()
        t0 = time.monotonic()
        next_arm = next_backup = next_uni = 0.0
        next_sample = 60.0
        try:
            while True:
                el = time.monotonic() - t0
                if el >= STRESS_S:
                    break
                if el >= next_arm:
                    w.send("?RTERM,START,20")
                    next_arm = el + 10
                if el >= next_backup:
                    w.send("?backup")
                    next_backup = el + 5
                if el >= next_uni:
                    t = marker("s")
                    w.send(f";W2,;S2{t}")
                    uni.append(t)
                    next_uni = el + 3
                if el >= next_sample:
                    samples.append((round(el), _radio_sample(w)))
                    next_sample = el + 60
                stall = next((x.rstrip() for x in w.dev.since(m0) if STALL_LINE in x), None)
                if stall:
                    stalled = (round(el), stall)
                    break
                time.sleep(0.25)
        finally:
            w.send("?RTERM,STOP")
            time.sleep(2.0)
        lines = [x.rstrip() for x in w.dev.since(m0)]
        stall = stalled or next(((None, x) for x in lines if STALL_LINE in x), None)
        end = _radio_sample(w)
        samples.append((round(time.monotonic() - t0), end))
        got = w2s2.received(um)
        delivered = sum(1 for t in uni if t.encode() in got)
        last_ok = False
        for _ in range(3):                                # delivery still works once the load is off
            t = marker("e")
            mk = w2s2.mark()
            w.send(f";W2,;S2{t}")
            try:
                w2s2.expect(t.encode(), timeout=5, since=mk)
                last_ok = True
                break
            except AssertionError:
                time.sleep(2.0)
        not_sent = sum(int(g.group(1)) for g in (re.match(r"^\[MESH\] (\d+) ESP-NOW frame\(s\) not sent", x)
                                                  for x in lines) if g)
        what = f"STALLED at {stall[0]} s: {stall[1][:160]}" if stall else "no stall"
        trend = [(s, h[0], h[1], h[2], h[3]) for s, h in samples]
        bench.note(f"etm.rterm_backup_stress: {what} in {samples[-1][0]} s; heap (s, free, largest, min since boot, "
                   f"not-sent total) {trend}; '[MESH] ... not sent' lines summed {not_sent}; "
                   f"unicasts to W2 delivered {delivered}/{len(uni)}; delivery after the load "
                   f"{'OK' if last_ok else 'FAILED'}")
        if stall:
            problems.append(f"W1 reported its ESP-NOW transmit stalled ({stall[1][:160]})")
        if not last_ok:
            problems.append("after the load a unicast from W1 no longer reached W2 S2")
        frees = [h[0] for _, h in samples]
        if min(frees) < 10240:
            problems.append(f"W1's free heap fell to {min(frees)} bytes")
        if frees[-1] < frees[0] - 4096:
            problems.append(f"W1's free heap drained from {frees[0]} to {frees[-1]} bytes")
        if stall or not last_ok:
            w.reboot()                                    # a wedged transmit stays wedged until a reboot (#112)
            rebooted = True
    if rebooted:
        bench.note("etm.rterm_backup_stress: W1 was rebooted after the run to clear its transmit")
    assert not problems, "; ".join(problems)
