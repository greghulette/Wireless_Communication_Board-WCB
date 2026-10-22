"""Channel sync: the whole mesh on one radio channel (docs/CHANNEL_SYNC.md; Code/WCB/WCB_ChanSync.{h,cpp}).

Every test here is opt-in `chsync` (hil/optin.py): the channel moves, so anything on the bench that does not take part
(firmware without channel sync, a NaviCore whose WCB_Client predates it) is off the mesh until the test moves back.
- chsync.status_line: ?CHSYNC's [CHSYNC:...] machine line on W1 and W2, and W1's [WDPCH:...] self line in ?WDP,DUMP.
- chsync.operator_move_and_back: ?CHSYNC,MOVE,<other> on W1 moves W1 and W2 together, and back. A board with its
  access point up applies a move by restarting (WCB_ChanSync.cpp applyMove), so W1 on this bench restarts twice.
- chsync.stranded_found: W2 is saved onto another channel and rebooted. It is alone there, its own leader, and misses
  W1; W1's group misses W2. Either search (W2's at about 50 s after its boot, the leader's 65 s after W2 went silent)
  brings W2 home; the test waits up to 150 s.
- chsync.region_refuses_12: the region checks of ?WCBCH and ?CHSYNC,MOVE under the default region "01", with ?REGION,DE
  saved for a moment so ?WCBCH,12 is accepted for the next boot, and both put back. Nothing reboots.

A restore if aborted, on each board's own USB console: ?REGION,<chain value> (01 on this bench), ?WCBCH,<chain value>,
?CHSYNC,ON, then ?reboot. A channel saved by a move is in each board's ?backup as ?WCBCH.
"""
import re
import time

from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import Watch, config_guard, link, marker, snapshot, token, usb_wcb
from suites.s03_wcb import _w2_online

CHSYNC_LINE = re.compile(r"^\[CHSYNC:EN=(\d),CH=(\d+),MAXCH=(\d+),CC=([0-9A-Z?]{2}),RANK=(\d+),PIN=(\d),LEAD=(\d+),"
                         r"LRANK=(\d+),LPIN=(\d),STATE=(idle|search|move)\]$")
FIELDS = ("EN", "CH", "MAXCH", "CC", "RANK", "PIN", "LEAD", "LRANK", "LPIN", "STATE")
SETTLE_S = 30.0       # a move: 1.5 s countdown, a deferred restart (4 s quiet) and a boot
STRANDED_S = 150.0    # W2's own search starts ~50 s after its boot (BOOT_GRACE_MS + TRIGGER_STABLE_MS)


def _has(lines, text):
    return any(text in x for x in lines)


def _w2(bench):
    if not bench.usb_wcbs().get(2):
        raise Skip("W2 has no USB console on this bench: a board on another channel is unreachable over the mesh")
    return WCB(bench.dev(bench.usb_wcbs()[2]))


def _chsync(w):
    """{field: value} from ?CHSYNC's machine line (WCB_ChanSync.cpp printStatus)."""
    for x in w.run("?CHSYNC", timeout=6):
        m = CHSYNC_LINE.match(x.rstrip())
        if m:
            return dict(zip(FIELDS, m.groups()))
    raise AssertionError("?CHSYNC printed no [CHSYNC:...] line (firmware without channel sync?)")


def _chain_channel(tokens, n):
    t = token(tokens, "?WCBCH,")
    if t is None:
        raise Skip(f"W{n}'s chain lacks ?WCBCH")
    return int(t.split(",")[1])


def _other(ch):
    """Five channels away (1->6, 6->11, 11->5): adjacent 20 MHz channels overlap, and at bench range a board one
    channel off still hears the mesh (s27 ident.channel_reboot)."""
    return (ch + 4) % 11 + 1


def _unicast(w1, w2s2, problems, when):
    t = marker()
    watch = Watch(w2s2)
    w1.send(f";W2,;S2{t}")
    try:
        watch.expect(w2s2, t.encode(), timeout=6)
    except AssertionError:
        problems.append(f"a unicast W1->W2 was not delivered {when}")


def _settle(w, mark, ch, timeout=SETTLE_S):
    """Wait until board w applied a move to channel ch: live ('[CHSYNC] moved from channel x to ch') or by a restart
    (its boot completes). -> 'live' | 'restart' | None (neither within timeout)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        lines = [x.rstrip() for x in w.dev.since(mark)]
        if any(re.match(rf"^\[CHSYNC\] moved from channel \d+ to {ch}$", x) for x in lines):
            return "live"
        if any(x.startswith("Rebooting now") for x in lines):
            w.wait_boot(mark, timeout=25)
            return "restart"
        time.sleep(0.5)
    return None


def _on_channel(w, ch, who, problems, when):
    st = _chsync(w)
    if st["CH"] != str(ch):
        problems.append(f"{who} reports CH={st['CH']} {when}, not {ch}")
    return st


@test("chsync.status_line", "?CHSYNC on W1 and W2 prints one [CHSYNC:EN=,CH=,MAXCH=,CC=,RANK=,PIN=,LEAD=,LRANK=,LPIN=,STATE=] line: on by default, CH the ?WCBCH of the board's own chain, CC its ?REGION (MAXCH 11 under 01), RANK its board number, not pinned unless it joins a network, STATE idle, and both name the same leader; W1's ?WDP,DUMP carries a [WDPCH:N=1,...] self line that agrees", needs=["wcb1", "wcb2"], links=[], opt_in="chsync")
def status_line(bench):
    """WCB_ChanSync.cpp printStatus and WCB_WDP.cpp wdpEmitDumpChan. Reads only."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    lead = {}
    for n, w in ((1, w1), (2, w2)):
        chain = snapshot(bench, n)
        st = _chsync(w)
        region = (token(chain, "?REGION,") or "?REGION,01").split(",")[1].upper()
        wifi = (token(chain, "?WIFI,") or "?WIFI,OFF").split(",")[1].upper()
        want = {"EN": "1", "CH": str(_chain_channel(chain, n)), "CC": region, "RANK": str(n), "STATE": "idle"}
        if region == "01":
            want["MAXCH"] = "11"
        if wifi != "JOIN":
            want["PIN"] = "0"
        bad = {k: (st[k], v) for k, v in want.items() if st[k] != v}
        if bad:
            problems.append(f"W{n}'s [CHSYNC] line: {bad} (got, wanted)")
        if token(chain, "?CHSYNC,") != "?CHSYNC,ON":
            problems.append(f"W{n}'s chain carries {token(chain, '?CHSYNC,')!r}, not ?CHSYNC,ON")
        lead[n] = (st["LEAD"], st["LRANK"])
        if n == 1:
            ch_line = next((x.rstrip() for x in w.run("?WDP,DUMP", timeout=8) if x.startswith("[WDPCH:N=1,")), None)
            wanted = (f"[WDPCH:N=1,CH={st['CH']},RANK=1,PIN={st['PIN']},SYNC=1,CC={st['CC']}]")
            if ch_line != wanted:
                problems.append(f"W1's [WDPCH] self line is {ch_line!r}, not {wanted!r}")
    if lead[1] != lead[2]:
        problems.append(f"W1 and W2 name different leaders: {lead[1]} vs {lead[2]} (LEAD, LRANK)")
    assert not problems, "; ".join(problems)


@test("chsync.operator_move_and_back", "?CHSYNC,MOVE,<other> on W1 moves W1 and W2 together, five channels away: each applies it (live, or by a restart when its access point is up), reports CH=<other>, and its chain holds ?WCBCH,<other>, and a unicast W1->W2 arrives there; ?CHSYNC,MOVE,<original> brings both back the same way, chains and a unicast checked again", needs=["wcb1", "wcb2"], links=["W2S2"], opt_in="chsync")
def operator_move_and_back(bench):
    """An operator move (MF_OPERATOR): every unpinned board with channel sync on obeys it whoever sent it
    (chansync::judgeMove). A move persists the channel (WCB_ChanSync.cpp persistChannel), so each chain changes with it
    and the move back restores it. If the move back does not land, the finally saves the original channel on each board
    over its own USB and reboots it."""
    w2s2 = link(bench, 2, "S2")
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems, how = [], {}
    with config_guard(bench, 1, 2) as before:
        orig = _chain_channel(before[1], 1)
        if _chain_channel(before[2], 2) != orig:
            raise Skip("W1 and W2 are saved on different channels")
        other = _other(orig)
        for n, w in ((1, w1), (2, w2)):
            st = _chsync(w)
            if st["PIN"] == "1":
                raise Skip(f"W{n} is joined to an access point: an operator move is refused while a pinned board is heard")
        moved = False
        try:
            m1, m2 = w1.dev.mark(), w2.dev.mark()
            out = [x.rstrip() for x in w1.run(f"?CHSYNC,MOVE,{other}")]
            if not _has(out, f"[CHSYNC] moving the mesh from channel {orig} to {other}"):
                raise AssertionError(f"?CHSYNC,MOVE,{other} was not started: {out[:4]}")
            moved = True
            how["out1"], how["out2"] = _settle(w1, m1, other), _settle(w2, m2, other)
            if how["out2"] is None and not _has(w2.dev.since(m2), f"moves the mesh to channel {other}"):
                problems.append(f"W2 never heard W1's MOVE to channel {other}")
            for n, w in ((1, w1), (2, w2)):
                _on_channel(w, other, f"W{n}", problems, "after the move")
                if f"?WCBCH,{other}" not in snapshot(bench, n):
                    problems.append(f"W{n}'s chain does not hold ?WCBCH,{other} after the move")
            time.sleep(3)                        # heartbeats on the new channel: W1 sees W2 online again
            _unicast(w1, w2s2, problems, f"on channel {other}")
        finally:
            if moved:
                m1, m2 = w1.dev.mark(), w2.dev.mark()
                out = [x.rstrip() for x in w1.run(f"?CHSYNC,MOVE,{orig}")]
                if not _has(out, f"to {orig} in"):
                    problems.append(f"?CHSYNC,MOVE,{orig} (the move back) was not started: {out[:4]}")
                how["back1"], how["back2"] = _settle(w1, m1, orig), _settle(w2, m2, orig)
                for n, w in ((1, w1), (2, w2)):
                    if _chsync(w)["CH"] != str(orig):
                        problems.append(f"W{n} did not move back to channel {orig}: saved and rebooted onto it")
                        w.run(f"?WCBCH,{orig}")
                        w.reboot()
        bench.note(f"chsync.operator_move_and_back: channel {orig} -> {other} -> {orig}, applied {how}")
        for n, w in ((1, w1), (2, w2)):
            _on_channel(w, orig, f"W{n}", problems, "after the move back")
        _w2_online(bench, w1)
        _unicast(w1, w2s2, problems, f"back on channel {orig}")
    assert not problems, "; ".join(problems)


@test("chsync.stranded_found", "W2 saved onto another channel (?WCBCH,<other> and a reboot) boots there alone and is found by a search - its own (it misses W1) or its group's leader's (W1's group misses W2) - and brought home within 150 s: W2 reports CH=<original>, its chain holds ?WCBCH,<original> again, a search line was printed, and a unicast W1->W2 arrives", needs=["wcb1", "wcb2"], links=["W2S2"], opt_in="chsync")
def stranded_found(bench):
    """docs/CHANNEL_SYNC.md §4. W2 alone on <other> is its own leader and misses W1: chansync::shouldSearch holds from
    its boot, and its first attempt comes BOOT_GRACE_MS + TRIGGER_STABLE_MS (50 s) after it. It probes the other
    channels, W1's group replies on <original>, and judgeSearch sends W2 there (a better leader) - or, if W1's group's
    leader searches first (65 s after W2 went silent), it pulls W2 home. Either way W2's channel is saved by the move.
    The polls here are 5 s apart: a search waits for 1 s without a command (CHSYNC_QUIET_MS, WCB.ino)."""
    w2s2 = link(bench, 2, "S2")
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    with config_guard(bench, 1, 2) as before:
        orig = _chain_channel(before[2], 2)
        if _chain_channel(before[1], 1) != orig:
            raise Skip("W1 and W2 are saved on different channels")
        other = _other(orig)
        stranded, home = False, False
        m1 = w1.dev.mark()
        try:
            out = [x.rstrip() for x in w2.run(f"?WCBCH,{other}")]
            if not _has(out, f"Mesh channel saved as {other}"):
                raise AssertionError(f"?WCBCH,{other} on W2 printed {out[:3]}")
            stranded = True
            bm = w2.reboot()
            _on_channel(w2, other, "W2", problems, "after its reboot")
            seen = w2.dev.mark()                 # past this boot: a later boot line is the move's restart
            t0 = time.monotonic()
            while time.monotonic() - t0 < STRANDED_S:
                time.sleep(5)
                if any(x.startswith("Rebooting now") for x in w2.dev.since(seen)):
                    w2.wait_boot(seen, timeout=25)   # W2 with its access point up moves by restarting
                    seen = w2.dev.mark()
                try:
                    if _chsync(w2)["CH"] == str(orig):
                        home = True
                        break
                except AssertionError:
                    continue                     # mid-restart
            took = time.monotonic() - t0
            # A search line on W1 or W2, or W2 following a pull from a leader the bench cannot read (NaviCore).
            searched = [x.rstrip() for x in w1.dev.since(m1) + w2.dev.since(bm)
                        if x.startswith("[CHSYNC] ") and ("search" in x or "moves the mesh to channel" in x)]
            bench.note(f"chsync.stranded_found: W2 home={home} after {took:.0f} s; lines {searched[:4]}")
            if not home:
                problems.append(f"W2 was not brought back to channel {orig} within {STRANDED_S:.0f} s")
            elif not searched:
                problems.append("W2 came home with no search or move line on W1 or W2")
        finally:
            if stranded and not home:
                w2.run(f"?WCBCH,{orig}")
                w2.reboot()
        if f"?WCBCH,{orig}" not in snapshot(bench, 2):
            problems.append(f"W2's chain does not hold ?WCBCH,{orig} at the end")
        _w2_online(bench, w1)
        _unicast(w1, w2s2, problems, f"after W2 came home to channel {orig}")
    assert not problems, "; ".join(problems)


@test("chsync.region_refuses_12", "Under the default region 01: ?REGION shows 'Region: 01, channels 1-11'; ?WCBCH,12 and ?WCBCH,13 are refused ('Valid range: 1-11.'), ?CHSYNC,MOVE,12 is refused (outside region 01) and ?REGION,XX is refused, each saving nothing; with ?REGION,DE saved (for the next boot) ?WCBCH,12 is accepted with a note; ?WCBCH,<original> and ?REGION,01 put both back, after which ?WCBCH,12 is refused again - no reboot", needs=["wcb1"], links=[], opt_in="chsync")
def region_refuses_12(bench):
    """chanSyncWcbchLimit (WCB_ChanSync.cpp): the applied region's top channel, or 13 while ?REGION has saved another
    one, which the next boot applies and checks the channel against (chanSyncApplyRegionAtBoot). Nothing here reboots,
    so the radio never leaves its channel; the finally puts ?WCBCH and ?REGION back from W1's own chain."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        reg = token(before[1], "?REGION,")
        if reg is None:
            raise Skip("W1's chain lacks ?REGION (firmware without channel sync)")
        if reg.upper() != "?REGION,01":
            raise Skip(f"W1 runs region {reg.split(',')[1]}, not the default 01")
        orig = _chain_channel(before[1], 1)
        touched = False
        try:
            out = [x.rstrip() for x in w.run("?REGION")]
            if "Region: 01, channels 1-11" not in out:
                problems.append(f"?REGION printed {out[:3]}")
            for ch in (12, 13):
                if not _has(w.run(f"?WCBCH,{ch}"), f"Invalid mesh channel {ch}. Valid range: 1-11."):
                    problems.append(f"?WCBCH,{ch} was not refused under region 01")
            if not _has(w.run("?CHSYNC,MOVE,12"), "[CHSYNC] channel 12 is outside region 01 (channels 1-11)"):
                problems.append("?CHSYNC,MOVE,12 was not refused under region 01")
            if not _has(w.run("?REGION,XX"), 'Unknown region "XX"'):
                problems.append("?REGION,XX was not refused")
            if snapshot(bench, 1) != before[1]:
                problems.append("a refused command changed W1's chain")
            touched = True
            if not _has(w.run("?REGION,DE"), "Region saved as DE - applies at the next boot"):
                problems.append("?REGION,DE did not confirm")
            out = [x.rstrip() for x in w.run("?WCBCH,12")]
            if not _has(out, "Mesh channel saved as 12"):
                problems.append(f"with region DE saved, ?WCBCH,12 was refused: {out[:3]}")
            if not _has(out, "the next boot applies region DE and checks it there"):
                problems.append("?WCBCH,12 under a pending region printed no note")
            if not _has(w.run("?REGION"), "Saved for the next boot: DE"):
                problems.append("?REGION does not show DE saved for the next boot")
        finally:
            if touched:
                w.run(f"?WCBCH,{orig}")
                w.run("?REGION,01")
        if not _has(w.run("?WCBCH,12"), "Invalid mesh channel 12. Valid range: 1-11."):
            problems.append("after ?REGION,01, ?WCBCH,12 was not refused again")
    assert not problems, "; ".join(problems)
