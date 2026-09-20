"""ETM: reboot announce and network characterisation. Slow — reboots boards and loads the mesh."""
import re
import time

from hil.runner import test
from suites.common import snapshot, usb_wcb


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


_CHAR = {}


def _char(bench):
    """Run ?ETM,CHAR once per bench session -> (recommended_ms, [(phase, wcb, avg, max, missed%)])."""
    if id(bench) not in _CHAR:
        w = usb_wcb(bench)
        m = w.send("?ETM,CHAR")
        rec = w.dev.expect(r"Recommended ETM timeout: (\d+)ms", timeout=180, since=m)
        time.sleep(1)
        rows, phase = [], None
        for t in w.dev.since(m):
            p = re.match(r"^ Phase (\d) - ", t)     # results block; progress lines have no leading space
            if p:
                phase = int(p.group(1))
            r = re.search(r"WCB(\d+): Min: \d+ms, Max: (\d+)ms, Avg: (\d+)ms, Missed: (\d+)%", t)
            if r and phase:
                rows.append((phase, int(r.group(1)), int(r.group(3)), int(r.group(2)), int(r.group(4))))
        bench.note(f"ETM CHAR recommended {rec.group(1)} ms; " +
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
