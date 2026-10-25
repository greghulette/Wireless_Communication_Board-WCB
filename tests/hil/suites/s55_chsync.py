"""Channel sync: the whole mesh on one radio channel (docs/CHANNEL_SYNC.md; Code/WCB/WCB_ChanSync.{h,cpp}).

Every test here is opt-in `chsync` (hil/optin.py): the channel moves, so anything on the bench that does not take part
(firmware without channel sync, a NaviCore whose WCB_Client predates 1.19.0) is off the mesh until the test moves back.
A NaviCore that takes part (NaviCore ab762c1, WCB_Client 1.19.0; its ?CHSYNC prints the same [CHSYNC:...] line) leads
the mesh at rank 0 (NaviCore.ino setup, setChanSyncRank(chansync::RANK_NAVICORE)) and follows every move: it saves the
channel into wcbNetwork.channel and, with its access point up, restarts to bring the AP up there (onMeshChannelMove).
- chsync.status_line: ?CHSYNC's [CHSYNC:...] machine line on W1 and W2, and W1's [WDPCH:...] self line in ?WDP,DUMP;
  with a NaviCore that takes part, its own line (rank 0) and both WCBs naming it their leader.
- chsync.operator_move_and_back: ?CHSYNC,MOVE,<other> on W1 moves W1 and W2 together, and back. A board with its
  access point up applies a move by restarting (WCB_ChanSync.cpp applyMove), so W1 on this bench restarts twice. A
  NaviCore that takes part moves too, inside nc_guard; with its access point up it restarts twice, so the test then
  needs navicore_reboot as well and skips while a restart would lose recorder state (NaviCore.restart_blocker).
- chsync.stranded_found: W2 is saved onto another channel and rebooted. It is alone there, its own leader, and misses
  W1; W1's group misses W2. Either search (W2's at about 50 s after its boot, the leader's 65 s after W2 went silent;
  the leader is NaviCore when it takes part, which never moves for it) brings W2 home; the test waits up to 150 s.
- chsync.region_refuses_12: the region checks of ?WCBCH and ?CHSYNC,MOVE under the default region "01", with ?REGION,DE
  saved for a moment so ?WCBCH,12 is accepted for the next boot, and both put back. Nothing reboots.

A restore if aborted, on each board's own USB console: ?REGION,<chain value> (01 on this bench), ?WCBCH,<chain value>,
?CHSYNC,ON, then ?reboot. A channel saved by a move is in each board's ?backup as ?WCBCH. NaviCore, on its USB:
?CHSYNC,MOVE,<original>, or a SET_CONFIG of {"wcbNetwork":{"channel":<original>}} and a REBOOT.
"""
import contextlib
import re
import time

from hil import optin
from hil.nc_guard import nc_guard
from hil.navicore import NaviCore
from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import Watch, config_guard, link, marker, snapshot, token, usb_wcb
from suites.s03_wcb import _w2_online
from suites.s45_navicore_wifi import ap_of

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


# ------------------------------------------------------------------ NaviCore, when it takes part
# NaviCore's restart for a move (NaviCore.ino onMeshChannelMove, then checkDeferredRestart's '[RC] restarting (<why>)'),
# or the restart itself as the harness sees it (the port reopened, setup() printing again).
NC_MOVE_RESTART = re.compile(r"^\[CHSYNC\] the mesh moved to channel \d+ - saved; restarting|"
                             r"^\[RC\] restarting \(the mesh moved channel|^<<reopened|^Reset reason: |^=== NaviCore ===$")


def _nc_chsync(nc):
    """NaviCore's ?CHSYNC machine line -> {field: value}, or None when its firmware has no channel sync ('Unknown command:
    ?CHSYNC') or its mesh is down. NaviCore.ino execCliLine '?CHSYNC' prints WCB_Client's printChanSyncStatus, which
    ends in the same [CHSYNC:...] line a WCB prints (WCB_Client.cpp, PIN always 0: NaviCore never joins a network)."""
    lines = nc.cli("?CHSYNC", until=r"^\[CHSYNC:|^Unknown command: \?CHSYNC|^\[CHSYNC\] the mesh is not up")
    for x in lines:
        m = CHSYNC_LINE.match(x.rstrip())
        if m:
            return dict(zip(FIELDS, m.groups()))
    return None


def _navicore_part(bench):
    """(a NaviCore driver, its ?CHSYNC fields) when the bench has a NaviCore that takes part in channel sync (EN=1),
    else (None, None), noted: one without channel sync, or with chanSync off, is left behind by a move until the move
    back."""
    if not bench.has("navicore"):
        return None, None
    nc = NaviCore(bench.dev("navicore"))
    st = _nc_chsync(nc)
    if st is None or st["EN"] != "1":
        bench.note("NaviCore does not take part in channel sync (" + ("no ?CHSYNC: firmware before NaviCore ab762c1"
                   if st is None else "chanSync off") + "): it is off the mesh while the others are on another channel")
        return None, None
    return nc, st


def _nc_settle(nc, mark, ch, timeout=SETTLE_S):
    """Wait until NaviCore runs on channel ch after a move announced after mark `mark` -> 'restart' (it saved the
    channel and restarted to bring its access point up there: onMeshChannelMove, NaviCore.ino), 'live' (no access
    point: WCB_Client switched the radio) or None. Polled with ?CHSYNC every 2 s, which also releases a line NaviCore's
    USB holds back; a poll that meets the restart is retried once it is back."""
    t0 = time.monotonic()
    restarted = False
    while time.monotonic() - t0 < timeout:
        restarted = restarted or any(NC_MOVE_RESTART.match(x.rstrip()) for x in nc.dev.since(mark))
        try:
            if restarted:
                nc.wait_boot(since=mark, timeout=max(5.0, timeout - (time.monotonic() - t0)))
            st = _nc_chsync(nc)
        except AssertionError:
            st = None
        if st and st["CH"] == str(ch):
            return "restart" if restarted else "live"
        time.sleep(2.0)
    return None


def _nc_saved_channel(nc):
    return (nc.config().get("wcbNetwork") or {}).get("channel")


@test("chsync.status_line", "?CHSYNC on W1 and W2 prints one [CHSYNC:EN=,CH=,MAXCH=,CC=,RANK=,PIN=,LEAD=,LRANK=,LPIN=,STATE=] line: on by default, CH the ?WCBCH of the board's own chain, CC its ?REGION (MAXCH 11 under 01), RANK its board number, not pinned unless it joins a network, STATE idle, and both name the same leader; W1's ?WDP,DUMP carries a [WDPCH:N=1,...] self line that agrees; a NaviCore that takes part prints its own line - rank 0, CH and CC its config's wcbNetwork channel and region - and, with no board joined to a network, leads: W1 and W2 name it, rank 0", needs=["wcb1", "wcb2"], links=[], opt_in="chsync")
def status_line(bench):
    """WCB_ChanSync.cpp printStatus and WCB_WDP.cpp wdpEmitDumpChan; NaviCore's ?CHSYNC (NaviCore.ino execCliLine,
    WCB_Client printChanSyncStatus) and its rank 0 (setup, setChanSyncRank(chansync::RANK_NAVICORE); docs/CHANNEL_SYNC.md
    §1: the best key present leads, and a pinned board beats any rank). Reads only."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    lead = {}
    nc, st = _navicore_part(bench)
    if nc is not None:
        net = nc.config().get("wcbNetwork") or {}
        want ={"EN": "1", "CH": str(net.get("channel")), "CC": net.get("region") or "01", "RANK": "0", "PIN": "0",
                "STATE": "idle"}
        if want["CC"] == "01":
            want["MAXCH"] = "11"
        bad = {k: (st[k], v) for k, v in want.items() if st[k] != v}
        if bad:
            problems.append(f"NaviCore's [CHSYNC] line: {bad} (got, wanted)")
        if st["LPIN"] == "0" and (st["LEAD"], st["LRANK"]) != (str(net.get("deviceId")), "0"):
            problems.append(f"NaviCore names leader {st['LEAD']} (rank {st['LRANK']}), not itself at rank 0")
        lead[0] = (str(net.get("deviceId")), "0")
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
        if 0 in lead and st["LPIN"] == "0" and lead[n] != lead[0]:
            problems.append(f"W{n} names leader {st['LEAD']} (rank {st['LRANK']}), not NaviCore "
                            f"{lead[0][0]} at rank 0, though no leader is joined to a network")
        if n == 1:
            ch_line = next((x.rstrip() for x in w.run("?WDP,DUMP", timeout=8) if x.startswith("[WDPCH:N=1,")), None)
            wanted = (f"[WDPCH:N=1,CH={st['CH']},RANK=1,PIN={st['PIN']},SYNC=1,CC={st['CC']}]")
            if ch_line != wanted:
                problems.append(f"W1's [WDPCH] self line is {ch_line!r}, not {wanted!r}")
    if lead[1] != lead[2]:
        problems.append(f"W1 and W2 name different leaders: {lead[1]} vs {lead[2]} (LEAD, LRANK)")
    assert not problems, "; ".join(problems)


@test("chsync.operator_move_and_back", "?CHSYNC,MOVE,<other> on W1 moves W1 and W2 together, five channels away: each applies it (live, or by a restart when its access point is up), reports CH=<other>, and its chain holds ?WCBCH,<other>, and a unicast W1->W2 arrives there; ?CHSYNC,MOVE,<original> brings both back the same way, chains and a unicast checked again; a NaviCore that takes part follows both moves (by restarting when its access point is up: navicore_reboot too), reports the channel and saves it in wcbNetwork.channel, and ends with its config as it began", needs=["wcb1", "wcb2"], links=["W2S2"], opt_in="chsync")
def operator_move_and_back(bench):
    """An operator move (MF_OPERATOR): every unpinned board with channel sync on obeys it whoever sent it
    (chansync::judgeMove). A move persists the channel (WCB_ChanSync.cpp persistChannel), so each chain changes with it
    and the move back restores it. If the move back does not land, the finally saves the original channel on each board
    over its own USB and reboots it.

    NaviCore with channel sync obeys it too (WCB_Client 1.19.0): onMeshChannelMove (NaviCore.ino) saves the channel into
    wcbNetwork.channel and, with its access point up, asks for the deferred restart that brings the AP up on the new
    channel; with none the library switches the radio live. So the test runs inside nc_guard (the two saves leave the
    config as it was, byte for byte, or the guard says what differs), and when NaviCore's config brings an access point
    up (s45 ap_of) it restarts twice: then it needs navicore_reboot and skips while a restart would lose recorder state.
    A NaviCore left on <other> is moved back on its own USB (?CHSYNC,MOVE,<original>: it is alone there, so only it
    moves), or saved onto <original> and restarted."""
    w2s2 = link(bench, 2, "S2")
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems, how = [], {}
    nc, _ = _navicore_part(bench)
    if nc is not None and ap_of(nc.config()) is not None:
        if "navicore_reboot" not in optin.enabled(bench.cfg):
            raise Skip("NaviCore takes part in channel sync with its access point up, so it follows each move by "
                       "restarting: " + optin.skip_reason("navicore_reboot"))
        why = nc.restart_blocker()
        if why:
            raise Skip(why)
    with config_guard(bench, 1, 2) as before, (nc_guard(bench) if nc is not None else contextlib.nullcontext()):
        orig = _chain_channel(before[1], 1)
        if _chain_channel(before[2], 2) != orig:
            raise Skip("W1 and W2 are saved on different channels")
        other = _other(orig)
        for n, w in ((1, w1), (2, w2)):
            st = _chsync(w)
            if st["PIN"] == "1":
                raise Skip(f"W{n} is joined to an access point: an operator move is refused while a pinned board is heard")
        if nc is not None and _nc_saved_channel(nc) != orig:
            raise Skip(f"NaviCore is saved on channel {_nc_saved_channel(nc)}, W1 and W2 on {orig}")
        moved = False
        try:
            m1, m2 = w1.dev.mark(), w2.dev.mark()
            mn = nc.dev.mark() if nc is not None else None
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
            if nc is not None:
                how["outN"] = _nc_settle(nc, mn, other)
                if how["outN"] is None:
                    problems.append(f"NaviCore did not follow the MOVE to channel {other} within {SETTLE_S:.0f} s")
                elif _nc_saved_channel(nc) != other:
                    problems.append(f"NaviCore runs on channel {other} but saved {_nc_saved_channel(nc)} "
                                    f"(onMeshChannelMove saves the move)")
            time.sleep(3)                        # heartbeats on the new channel: W1 sees W2 online again
            _unicast(w1, w2s2, problems, f"on channel {other}")
        finally:
            if moved:
                m1, m2 = w1.dev.mark(), w2.dev.mark()
                mn = nc.dev.mark() if nc is not None else None
                out = [x.rstrip() for x in w1.run(f"?CHSYNC,MOVE,{orig}")]
                if not _has(out, f"to {orig} in"):
                    problems.append(f"?CHSYNC,MOVE,{orig} (the move back) was not started: {out[:4]}")
                how["back1"], how["back2"] = _settle(w1, m1, orig), _settle(w2, m2, orig)
                for n, w in ((1, w1), (2, w2)):
                    if _chsync(w)["CH"] != str(orig):
                        problems.append(f"W{n} did not move back to channel {orig}: saved and rebooted onto it")
                        w.run(f"?WCBCH,{orig}")
                        w.reboot()
                if nc is not None:
                    how["backN"] = _nc_settle(nc, mn, orig)
                    if how["backN"] is None:
                        problems.append(f"NaviCore did not follow the move back to channel {orig}: moved back on its "
                                        f"own USB")
                        mn = nc.dev.mark()
                        try:
                            nc.cli(f"?CHSYNC,MOVE,{orig}", until=r"^\[CHSYNC\] (moving the mesh|move refused)")
                        except AssertionError:
                            pass                 # settled below either way
                        if _nc_settle(nc, mn, orig) is None:
                            nc.set_config({"wcbNetwork": {"channel": orig}})
                            if "navicore_reboot" in optin.enabled(bench.cfg):
                                nc.reboot("json")
                            else:
                                problems.append(f"NaviCore is still on channel {other}, saved on {orig}: it needs a "
                                                f"restart (REBOOT on its USB)")
                    if _nc_saved_channel(nc) != orig:
                        problems.append(f"NaviCore's saved channel is {_nc_saved_channel(nc)} after the move back, "
                                        f"not {orig}")
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
    The polls here are 5 s apart: a search waits for 1 s without a command (CHSYNC_QUIET_MS, WCB.ino). With a NaviCore
    that takes part, it leads W1's group (rank 0): its own search lines ('[WCB_Client] channel sync: ...', WCB_Client.cpp)
    count, and it must still be on <original> at the end - a stray board is brought to the leader, never the reverse.
    One with a station on its access point does not search (docs/CHANNEL_SYNC.md §8), so W2's own search does it."""
    w2s2 = link(bench, 2, "S2")
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    nc, _ = _navicore_part(bench)
    with config_guard(bench, 1, 2) as before:
        orig = _chain_channel(before[2], 2)
        if _chain_channel(before[1], 1) != orig:
            raise Skip("W1 and W2 are saved on different channels")
        other = _other(orig)
        stranded, home = False, False
        m1 = w1.dev.mark()
        mn = nc.dev.mark() if nc is not None else None
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
            # A search line on W1 or W2, or W2 following a pull from its leader; NaviCore's own when it leads.
            searched = [x.rstrip() for x in w1.dev.since(m1) + w2.dev.since(bm)
                        if x.startswith("[CHSYNC] ") and ("search" in x or "moves the mesh to channel" in x)]
            if nc is not None:
                searched += [x.rstrip() for x in nc.dev.since(mn) if x.startswith("[WCB_Client] channel sync: ")
                             and ("searching" in x or "taking this group" in x or "pulling" in x)]
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
        if nc is not None:
            st = _nc_chsync(nc)
            if st is None or st["CH"] != str(orig) or _nc_saved_channel(nc) != orig:
                problems.append(f"NaviCore, the leader, left channel {orig} (it reports "
                                f"CH={st['CH'] if st else '?'}, saved {_nc_saved_channel(nc)})")
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
