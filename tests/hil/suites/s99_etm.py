"""ETM: reboot announce and network characterisation. Slow — reboots boards and loads the mesh.

The deferred-restart tests (tracker #93) reboot W1 once each and move nothing: W2 re-arms W1's remote terminal or
reports stats to it, and W1's own console asks for its version. Restore if aborted: `;W1,?RTERM,STOP` on W2's console
(the session and ?DEBUG,MGMT are RAM only, and W1's queued ?reboot clears both anyway).
"""
import re
import time

from hil.runner import test
from hil.wcb import WCB
from suites.common import remote_wcbs, snapshot, usb_wcb


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


def _peers_online(bench, w, wait=25.0, strict=True):
    """Wait until W1's ETM table shows every other bench WCB online -> the seconds it took. A WCB that has just booted
    treats every peer as offline until that peer's next packet, a whole heartbeat away at worst (HB 10 +/- 1 s): nothing
    answers a boot announce (the heartbeat branch of espNowReceiveCallback, WCB.ino). Until then ?ETM,CHAR aborts with
    'no online peers' (etm.char_unicast, run 20260924-234056) and a unicast to the peer goes out once with no ETM retry.
    ?WDP,POLL has every peer advertise within about a second, and any packet marks its sender online. strict=False only
    notes a peer still offline, for a finally that must not replace the test's own failure."""
    others, t0 = remote_wcbs(bench), time.monotonic()
    w.run("?WDP,POLL")
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
    _peers_online(bench, w, strict=False)


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
    """Run ?ETM,CHAR once per bench session -> (recommended_ms, [(phase, wcb, avg, max, missed%)]). It measures only
    the peers W1 sees online, so it waits for them first (_peers_online), and an abort fails at once instead of
    running out the 180 s."""
    if id(bench) not in _CHAR:
        w = usb_wcb(bench)
        waited = _peers_online(bench, w)
        m = w.send("?ETM,CHAR")
        rec = w.dev.expect(r"Recommended ETM timeout: (\d+)ms|\[ETM\] Characterization aborted: (.*)", timeout=180,
                           since=m)
        assert rec.group(1), f"?ETM,CHAR aborted: {rec.group(2).strip()}"
        time.sleep(1)
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
        _CHAR[id(bench)] = (int(rec.group(1)), rows)
    return _CHAR[id(bench)]


@test("etm.char_unicast", "?ETM,CHAR phases 1-2 (unicast, no load) lose under 5% to every peer", needs=["wcb1"])
def char_unicast(bench):
    _, rows = _char(bench)
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
    _, rows = _char(bench)
    rows = [r for r in rows if r[0] == 3]
    assert rows, "no phase 3 rows in ?ETM,CHAR output"
    lossy = [f"W{b} missed {ms}%" for _, b, _, _, ms in rows if ms > 5]
    assert not lossy, (f"phase 3 lossy: {lossy} — 30% is exactly the broadcast share; "
                       "broadcasts suppressed by lastReceivedViaESPNOW (WCB.ino:2549)?")
