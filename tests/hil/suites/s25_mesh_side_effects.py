"""Two mesh behaviours nothing else reaches: WDP PWM self-heal after a MISSED explicit clear (docs/HIL_TEST_AUDIT.md
A17 / WP5) and W1's RC telemetry relay window (WP6).

The self-heal (reconcileWdpAutoPWMOutputs, WCB_PWM.cpp, called from the WDP advert decode in WCB_WDP.cpp) clears an
output a board auto-configured from a neighbour's PWMTARGET advert when a later advert from that neighbour no longer
names the port. On a healthy mesh the explicit ?MAP,PWM,CLEAR,OUT + ?REBOOT that W1 sends always wins, so the branch
never ran in any test. Here W2 is made deaf for the few seconds W1 sends that clear - its `?MAC,3` receive filter is
flipped on its own USB console, the same trick s18's `_deaf_w1` uses on W1 - and restored before W1's next advert.

Restore if aborted: on W2's console `?MAC,3,<its chain value>`, then `?MAP,PWM,CLEAR,OUT,S3` there (deferred reboot),
and `?MAP,PWM,CLEAR,ALL` on W1. Nothing here moves a servo.

The WCB-WP28 tests at the end add WDP's Maestro auto-add (a proxy re-learnt into the slot it left, a baud refresh, a
full table, a proxy beside a local Maestro of the same id), the RC relay's OTA pause and its counted drops, and PWM
provenance across reboots (a port's prior broadcast flags, the auto-source tag, a manual output self-heal never
touches). Restore if aborted: W1 `?WDP,ON`, `?DEBUG,OFF`, `?MAESTRO,M2:W2S1:<its baud>` if W1's chain lost it, and
`?MAESTRO,CLEAR,M<n>` for any placeholder on WCB11+; on W2 `?MAESTRO,CLEAR,M1:W2S<p>` if it hosts a Maestro 1, then
`?MAESTRO,CLEAR,M1:W2S1` on W1.
"""
import re
import time

from hil.ncmesh import deaf as deafened      # the local flag named 'deaf' below is another thing
from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import Console, config_guard, link, nonce, require_tokens, snapshot, token, usb_wcb
from suites.s03_wcb import _w2_online
from suites.s14_pwm import (_clear_local_mapping, _clear_remote_out, _dump_cap, _has, _inline_clear_out, _no_pwm,
                            _pwm_reboot, _w2_reboot_wait)
from suites.s19_client_mesh import _client
from suites.s22_maestro_kyber import _m_lines, _port_devices, _slot


@test("pwm.wdp_selfheal_missed_clear", "(WP5) A WDP-auto-configured PWM output on W2 is cleared by W1's next advert, without a W2 reboot, when W2 missed W1's explicit clear (W1 x2 reboots, W2 deaf for a few seconds)", needs=["wcb1"], links=["W1S3"])
def wdp_selfheal_missed_clear(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    if "?WDP,OFF" in bench.config_tokens(1) or "?WDP,OFF" in bench.config_tokens(2):
        raise Skip("WDP is off on W1 or W2")
    bad = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        mac2 = token(before[2], "?MAC,3,")
        if mac2 is None:
            raise Skip("W2's chain lacks ?MAC,3")
        orig = mac2[len("?MAC,3,"):]
        other = "%02X" % (int(orig, 16) ^ 0x01)
        deaf = False
        try:
            # 1. W1 drives W2 S3; W2 takes the manual output the setup command sends, then loses it to a direct clear
            #    and gets it back as a WDP-auto-configured output (tagged with source W1), like s14's self-heal test.
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,W2S3")
            _pwm_reboot(w, m)
            assert "?MAP,PWM,OUT,S3" in snapshot(bench, 2), "setup: W2 did not take the output"
            time.sleep(5.0)
            for _ in range(3):
                _clear_remote_out(w, "S3")
                if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
                    break
            assert "?MAP,PWM,OUT,S3" not in snapshot(bench, 2), "setup: W2 still declares S3 after the direct clear"
            cm = c2.mark()
            assert _has(w.run("?WDP,POLL"), "[WDP] polled")
            c2.expect(r"\[WDP\] WCB1 drives our S3 .* auto-configuring PWM output", timeout=8, since=cm)
            time.sleep(2.0)
            assert "?MAP,PWM,OUT,S3" in snapshot(bench, 2), "setup: WDP auto-config did not declare W2 S3"

            # 2. W2 goes deaf: the explicit clear W1 is about to send never arrives. W1 must see W2 online first:
            #    a peer is offline to a W1 that has just rebooted until its next packet, and a unicast to an offline
            #    peer goes out once, untracked, so no 'failed to ACK' would ever come (run 20260928-220200).
            _w2_online(bench, w)
            out = _crun(c2, f"?MAC,3,{other}")
            if not _has(out, f"Updated 3rd MAC octet to 0x{other}"):
                raise AssertionError(f"W2 did not take ?MAC,3,{other}: {out}")
            deaf = True
            w.run("?DEBUG,ETM,ON")
            wm = w.dev.mark()
            m = w.send("?MAP,PWM,CLEAR,ALL")
            w.dev.expect(r"All PWM mappings cleared", timeout=3, since=m)
            try:
                w.dev.expect(r"\[ETM\] WCB2 failed to ACK seq \d+ after 3 retries: \?MAP,PWM,CLEAR,OUT,S3", timeout=8, since=wm)
            except AssertionError:
                bad.append("W1's explicit clear to W2 was not seen to fail (W2 not deaf?)")
            _pwm_reboot(w, m)
            w.run("?DEBUG,ETM,OFF")

            # 3. Still deaf, W2 must still declare the output: the explicit clear never landed. Read it and take the marks
            #    now, before W2 can hear. W1 has just rebooted, so its WDP boot burst (3 adverts at wdpBegin +1.6/+2.9/+4.2 s,
            #    WCB_WDP.cpp:1965-1966, :378-381) is still going when the octet comes back, and W2 decodes adverts in
            #    loop() (drainWdpPackets, WCB.ino:1217), so a burst advert heals W2 while a console command runs and the line
            #    lands just after it. 20260924-190733 lost that race: the self-heal line landed at the snapshot's HILEND,
            #    before the mark, and the poll found nothing left to clear. While deaf, adverts are dropped in the receive
            #    callback's octet check (WCB.ino:4899), not queued, and ?backup is read on W2's own USB.
            if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
                bad.append("W2 lost the output while deaf: the explicit clear got through, so this is not the self-heal")
            cm, wm = c2.mark(), w.dev.mark()

            # W2 hears again. W1's adverts name no PWMTARGET for W2: the self-heal must clear the output.
            out = _crun(c2, f"?MAC,3,{orig}")
            if not _has(out, f"Updated 3rd MAC octet to 0x{orig}"):
                raise AssertionError(f"W2 did not take its octet back: {out}")
            deaf = False
            assert _has(w.run("?WDP,POLL"), "[WDP] polled")
            try:
                c2.expect(r"\[WDP\] WCB1 no longer drives our S3 - clearing auto-configured PWM output", timeout=10, since=cm)
            except AssertionError:
                bad.append("W2 printed no self-heal line after W1's adverts without the PWMTARGET")
            time.sleep(2.0)
            if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                bad.append("W2 still declares S3 after the self-heal")
            if _has(w.dev.since(wm), "WCB2 came ONLINE (boot)"):
                bad.append("the self-heal rebooted W2 (it must clear live)")
            if _dump_cap(w, 2) is not None and _dump_cap(w, 2) & 0x0020:
                bad.append("W2 still advertises the PWM capability after the self-heal")
        finally:
            if deaf:
                _crun(c2, f"?MAC,3,{orig}")
            s3.pwm_out(0)
            if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                _clear_remote_out(w, "S3")
            if any(t.startswith("?MAP,PWM") for t in snapshot(bench, 1)):
                _clear_local_mapping(w, "S3")
            s3.pwm_stop()
    assert not bad, "; ".join(bad)


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


@test("navicore.rc_relay_window", "(WP6) A ;W20,{json} opens W1's 20 s RC telemetry relay: NaviCore's rc_hb / rc_ch lines reach W1 USB as {\"sys\":1,...} while it is open, and stop once it lapses (~35 s)", needs=["wcb1", "navicore"], links=[])
def rc_relay_window(bench):
    """rcJsonRelaySubscribedUntilMs (WCB.ino): any JSON payload routed to the controller renews a 20 s window; a plain
    text route (;W20,text) never does. rc_hb comes every few seconds and rc_ch at up to 5 Hz while the SBUS stream
    changes, so at least one telemetry line in 6 s proves the relay; none in the 6 s after the window lapsed proves the
    close. WCB.run() filters these lines, so the console is read directly."""
    w = usb_wcb(bench)
    time.sleep(21)                                   # let any earlier test's window lapse first
    m = w.dev.mark()
    time.sleep(6)
    stray = [x for x in w.dev.since(m) if x.startswith('{"sys":1')]
    assert not stray, f"telemetry was relayed with no window open: {stray[:2]}"
    m = w.dev.mark()
    w.send(";W20,HILtext")                           # a text route must not open the window
    time.sleep(6)
    stray = [x for x in w.dev.since(m) if x.startswith('{"sys":1')]
    assert not stray, f"a plain text route opened the relay: {stray[:2]}"
    m = w.dev.mark()
    w.send(';W20,{"type":"PING"}')
    t0 = time.monotonic()
    w.dev.expect(r'^\{"sys":1,"type":"PONG"', timeout=4, since=m)
    time.sleep(6)
    lines = [x for x in w.dev.since(m) if x.startswith('{"sys":1')]
    kinds = sorted({k for x in lines for k in re.findall(r'"type":"(rc_\w+)"', x)})
    bench.note(f"relayed in 6 s after the PING: {len(lines)} lines, types {kinds}")
    assert kinds, f"no rc_* telemetry relayed inside the window: {lines[:3]}"
    for x in lines:
        assert x.startswith('{"sys":1,"type":"'), f"a relayed line is not tagged sys:1 first: {x[:60]}"
    time.sleep(max(0.0, t0 + 22 - time.monotonic()))
    m = w.dev.mark()
    time.sleep(6)
    late = [x for x in w.dev.since(m) if x.startswith('{"sys":1')]
    assert not late, f"telemetry still relayed {round(time.monotonic() - t0)} s after the last JSON: {late[:2]}"


# ============================================================ WDP and relay side effects (WCB-WP28)
# Row 1: a WCB peer's MAESTRO_CFG advert auto-adds a persisted proxy per host (maestroAutoAddRemote,
# WCB_Maestro.cpp:906-941), gated on auto-join, a non-temporary sender, and a live peer or the controller
# (WCB_WDP.cpp:724-733). A proxy is always placed in the LOWEST free slot (findEmptySlot, :925), and the backup lists
# slots in order (emitMaestroBackup, :1123-1162), so a proxy cleared and learnt back lands where it was only when every
# slot below it is taken. Each test here checks that first, from W1's own report of the proxy's slot, and skips rather
# than reorder W1's table (D38: rebuilding it is itself a bench risk). Row 1's last check - W2's DUMP row lists its
# local Maestros only - is wdp.dump_neighbor_fields (s18).
AUTO_ADDED = r"^\[WDP\] auto-added remote Maestro {mid} on WCB{host} @ (\d+) baud \(slot (\d+)\)$"


def _w2(bench):
    return WCB(bench.dev("wcb2"))


def _slot_of(w, line):
    """The slot W1 keeps a ?MAESTRO line in. Re-issuing a configured line updates its slot in place and names it
    (configureMaestro keys on (id, port, host), WCB_Maestro.cpp:772-784, and prints '(unicast, slot n)' or
    '(slot n)', :864-880), so this changes nothing -> the slot number, or None."""
    for x in w.run(line):
        m = re.search(r"slot (\d+)\)", x)
        if m and "Maestro" in x:
            return int(m.group(1))
    return None


POLL_TRIES, POLL_EACH_S = 3, 3.5     # _poll_expect: an answered poll comes back within about a second


def _poll_expect(w, pattern, tries=POLL_TRIES, each_s=POLL_EACH_S):
    """?WDP,POLL on W1, then the line `pattern` that a peer's answering advert brings -> (the match or None, polls
    sent). W1's solicit and the peer's advert are each one unacknowledged broadcast frame, and the advert after a lost
    one is the 60 s backstop (WCB_WDP.cpp wdpTick), so a poll the line does not follow in `each_s` is sent again; the
    line may answer any of them. maestro.wdp_auto_add_readd_rebaud failed on one lost answer in run 20261006-235930
    (its next poll, 10 s later, was answered in 110 ms)."""
    wm = w.dev.mark()
    for n in range(1, tries + 1):
        w.run("?WDP,POLL")
        try:
            return w.dev.expect(pattern, timeout=each_s, since=wm), n
        except AssertionError:
            continue
    return None, tries


def _wdp_gates(bench, *wcbs):
    """Skip unless WDP runs on every board named and W1 auto-joins: with either off, no Maestro is auto-added."""
    for n in wcbs:
        toks = bench.config_tokens(n, refresh=True)
        if "?WDP,OFF" in toks:
            raise Skip(f"W{n}'s WDP is off")
        if n == 1 and "?WDP,AUTOJOIN,OFF" in toks:
            raise Skip("W1's WDP auto-join is off, so it auto-adds no Maestro (WCB_WDP.cpp:725)")


def _m2_proxy(bench):
    """(W1's M2:W2 proxy line, its (port, baud), its index in W1's slot order, W2's local Maestro 2 (port, baud)), or
    Skip. The two bauds must agree: W2's next advert would re-baud the proxy anyway."""
    l1, l2 = _m_lines(bench.config_tokens(1, refresh=True)), _m_lines(bench.config_tokens(2, refresh=True))
    host = _slot(l2, 2, 2)
    if not host:
        raise Skip("W2 hosts no local Maestro 2")
    proxy = _slot(l1, 2, 2)
    if not proxy:
        raise Skip("W1 has no M2:W2 proxy to learn back")
    if proxy[1] != host[1]:
        raise Skip(f"W1's M2:W2 proxy is at {proxy[1]} baud but W2's Maestro 2 at {host[1]}")
    line = f"?MAESTRO,M2:W2S{proxy[0]}:{proxy[1]}"
    idx = next(i for i, t in enumerate(l1) if t.upper() == line.upper())
    return line, proxy, idx, host, l1


@test("maestro.wdp_auto_add_readd_rebaud", "W1's M2:W2 proxy, cleared, is learnt back from W2's next advert into the slot it left ('[WDP] auto-added remote Maestro 2 on WCB2 @ <b> baud (slot n)') and the chain is as before; re-issued at another baud, the next advert puts W2's baud back ('baud updated to <b>') (WDP on; no reboot)", needs=["wcb1", "wcb2"], links=[])
def wdp_auto_add_readd_rebaud(bench):
    """WCB-WP28 row 1, steps 1-2 (maestro.wdp_auto_add, wdp.autoconfig.maestro_from_wcb_peer). A cleared remote proxy
    frees its slot with no port housekeeping (_clearMaestroSlot, WCB_Maestro.cpp:948-990: a proxy's port is 0), and
    ?WDP,POLL has W2 advertise within about a second (WCB_WDP.cpp:1873-1878). The auto-add claims the lowest free slot
    and prints its line (WCB_Maestro.cpp:925-939); an existing (id, host) proxy only has its baud refreshed, and says so
    (:910-917). A remote proxy is never advertised (only local slots are, WCB_WDP.cpp:156-167), so nothing outside W1
    changes. W1 is left exactly as found: the proxy goes back into its own slot by construction (see the section)."""
    w = usb_wcb(bench)
    _wdp_gates(bench, 1, 2)
    line, proxy, idx, host, lines = _m2_proxy(bench)
    target = f"M2:W2S{proxy[0]}"
    bad = []
    with config_guard(bench, 1):
        k = _slot_of(w, line)
        if k != idx + 1:
            raise Skip(f"W1's M2:W2 proxy sits in slot {k} with a free slot below it: learnt back, it would move and "
                       f"reorder W1's table")
        try:
            out = [x.rstrip() for x in w.run(f"?MAESTRO,CLEAR,{target}")]
            if f"Cleared Maestro {target} (freed slot {k})" not in out:
                raise AssertionError(f"?MAESTRO,CLEAR,{target} printed {out}")
            got, polls = _poll_expect(w, AUTO_ADDED.format(mid=2, host=2))
            if got is None:
                bad.append(f"W1 did not learn the cleared M2:W2 proxy back from W2's advert ({polls} polls)")
            elif (int(got.group(1)), int(got.group(2))) != (host[1], k):
                bad.append(f"the proxy was learnt back at {got.group(1)} baud in slot {got.group(2)}, expected "
                           f"{host[1]} in slot {k}")
            if _m_lines(snapshot(bench, 1)) != lines:
                bad.append("W1's Maestro table is not as before after the auto-add")
            alt = 9600 if host[1] != 9600 else 19200
            out = [x.rstrip() for x in w.run(f"?MAESTRO,{target}:{alt}")]
            if f"✓ Maestro 2: Remote on WCB2 (unicast, slot {k})" not in out:
                bad.append(f"re-issuing the proxy at {alt} baud printed {out}")
            got, polls = _poll_expect(w, rf"^\[WDP\] Maestro 2 @ WCB2 baud updated to {host[1]}$")
            if got is None:
                bad.append(f"W2's next advert did not put the proxy back to {host[1]} baud ({polls} polls)")
            if _m_lines(snapshot(bench, 1)) != lines:
                bad.append("W1's Maestro table is not as before after the baud refresh")
        finally:
            if line.upper() not in [t.upper() for t in _m_lines(snapshot(bench, 1))]:
                w.run(line)      # updates the proxy's baud in place, or re-adds it into slot k, the lowest free one
    assert not bad, "; ".join(bad)


@test("maestro.wdp_auto_add_table_full", "With every one of W1's 9 Maestro slots taken (placeholders on WCB11+) and its M2:W2 proxy cleared, W2's advert is heard but not added: '[WDP] Maestro 2 @ WCB2 heard but no free slot (max 9)' under ?DEBUG,ON, and no auto-added line; the proxy goes back into its own slot and the placeholders are cleared (WDP off while the table changes; no reboot)", needs=["wcb1", "wcb2"], links=[])
def wdp_auto_add_table_full(bench):
    """WCB-WP28 row 1, step 4. maestroAutoAddRemote prints the refusal only under ?DEBUG,ON (WCB_Maestro.cpp:925-930).
    The placeholders are remote proxies to boards that do not exist - never advertised, never sent anything - and WDP
    is off while they are added and removed (as in maestro.slot_map_capacity_clear_variants), so no real advert can
    take a slot mid-way; it is on for the one advert the test needs. Order is kept by construction: the proxy's own
    slot is freed last and refilled first."""
    w = usb_wcb(bench)
    _wdp_gates(bench, 1, 2)
    line, proxy, idx, host, lines = _m2_proxy(bench)
    pid = next((i for i in range(8, 2, -1) if not any(re.match(rf"^\?MAESTRO,M{i}:", t, re.I) for t in lines)), None)
    if pid is None:
        raise Skip("W1 uses every Maestro id from 3 to 8, so no placeholder id is free")
    hosts = list(range(11, 11 + (9 - len(lines)) + 1))     # the free slots, then the proxy's own
    target = f"M2:W2S{proxy[0]}"
    bad, filled = [], []
    with config_guard(bench, 1) as before:
        if "?WDP,OFF" in before[1]:
            raise Skip("W1's WDP is off")
        k = _slot_of(w, line)
        if k != idx + 1:
            raise Skip(f"W1's M2:W2 proxy sits in slot {k} with a free slot below it: refilled, the table would reorder")
        try:
            if not _has(w.run("?WDP,OFF"), "[WDP] disabled"):
                raise AssertionError("?WDP,OFF did not confirm")
            for h in hosts[:-1]:
                if not _has(w.run(f"?MAESTRO,M{pid}:W{h}S1:9600"), f"✓ Maestro {pid}: Remote on WCB{h} (unicast, slot "):
                    raise AssertionError(f"W1 did not take the placeholder M{pid}:W{h}")
                filled.append(h)
            out = [x.rstrip() for x in w.run(f"?MAESTRO,CLEAR,{target}")]
            if f"Cleared Maestro {target} (freed slot {k})" not in out:
                raise AssertionError(f"?MAESTRO,CLEAR,{target} printed {out}")
            if not _has(w.run(f"?MAESTRO,M{pid}:W{hosts[-1]}S1:9600"), f"(unicast, slot {k})"):
                raise AssertionError(f"the last placeholder did not take the proxy's slot {k}")
            filled.append(hosts[-1])
            listed = [x for x in w.run("?MAESTRO,LIST") if x.startswith("  Maestro ")]
            if len(listed) != 9:
                bad.append(f"the table should be full: ?MAESTRO,LIST shows {len(listed)} slots")
            w.run("?DEBUG,ON")
            if not _has(w.run("?WDP,ON"), "[WDP] enabled"):
                raise AssertionError("?WDP,ON did not confirm")
            wm = w.dev.mark()
            got, polls = _poll_expect(w, r"^\[WDP\] Maestro 2 @ WCB2 heard but no free slot \(max 9\)")
            if got is None:
                bad.append(f"no 'Maestro 2 @ WCB2 heard but no free slot (max 9)' line under ?DEBUG,ON ({polls} polls)")
            if any(re.match(AUTO_ADDED.format(mid=2, host=2), x) for x in w.dev.since(wm)):
                bad.append("W1 auto-added Maestro 2 into a full table")
        finally:
            w.run("?DEBUG,OFF")
            w.run("?WDP,OFF")
            if hosts[-1] in filled:
                w.run(f"?MAESTRO,CLEAR,M{pid}:W{hosts[-1]}S1")      # frees slot k, the only free one
            if line.upper() not in [t.upper() for t in _m_lines(snapshot(bench, 1))]:
                w.run(line)
            if filled:
                w.run(f"?MAESTRO,CLEAR,M{pid}")
            w.run("?WDP,ON")
    assert not bad, "; ".join(bad)


def _free_port(tokens, wcb, other_tokens=()):
    """The first of S2-S5 on W<wcb> that runs 9600 with nothing on it - no device, Maestro, PWM (s22 _port_devices),
    serial mapping or Kyber naming it, and no mapping on the other board (`other_tokens`) aimed at W<wcb>S<p> - so a
    Maestro placed there and cleared again leaves it as it was: the clear re-enables the port's broadcasts and resets
    it to 9600 (_clearMaestroSlot, WCB_Maestro.cpp:961-990). Or None."""
    up = [t.upper() for t in tokens]
    named = [t for t in up if t.startswith(("?MAP,", "?KYBER", "?HCR", "?MP3", "?DFP", "?WLED", "?MAESTRO"))]
    aimed = [t.upper() for t in other_tokens if t.upper().startswith("?MAP,")]
    for p in ("S2", "S3", "S4", "S5"):
        if f"?BAUD,{p},9600" not in up or _port_devices(tokens, p, wcb):
            continue
        if any(re.search(rf"{p}(?!\d)", t) for t in named) or any(f"W{wcb}{p}" in t for t in aimed):
            continue
        return p
    return None


@test("maestro.wdp_auto_add_beside_local", "A Maestro 1 placed on a free W2 port is auto-added on W1 as a proxy to WCB2 beside W1's own local Maestro 1 (per host, not first-host-wins; rule 5); W2's port takes the Maestro's baud and loses its broadcasts until the clear; W1 keeps the proxy after W2 stops hosting it (never evicted) until it is cleared, and does not learn it again (WDP on; no reboot)", needs=["wcb1", "wcb2"], links=[])
def wdp_auto_add_beside_local(bench):
    """WCB-WP28 row 1, step 3 (CLAUDE.md rule 5). maestroAutoAddRemote has no 'already configured elsewhere' guard:
    each advertising host gets its own (id, host) slot even when the id is local (WCB_Maestro.cpp:921-941). The local
    add on W2 moves its port to the Maestro's baud and turns both broadcast flags off (configureMaestro,
    WCB_Maestro.cpp:838-860); its clear gives them back and resets the port to 9600 (:961-990). HIL_TESTING.md §1 says
    no test adds a local Maestro id while WDP is on, because every board auto-adds a proxy for it and none is ever
    evicted. Here the auto-add is the subject, and it is undone in order: W2 stops hosting first, then W1's proxy is
    cleared, and a POLL proves W1 does not learn it again. Only a WCB auto-adds (WCB_Client emits MAESTRO_CFG but adds
    nothing, WCB_Client.cpp:1880-1985), so W1's proxy is the only one made. 19200 keeps a soft port within its range
    (:822-835). The plan's 'KyberRemoteTask drains it' check is left out: text into that port would ride W2's Kyber
    bridge to the real Maestros."""
    w, w2 = usb_wcb(bench), _w2(bench)
    _wdp_gates(bench, 1, 2)
    t1, t2 = bench.config_tokens(1, refresh=True), bench.config_tokens(2, refresh=True)
    l1, l2 = _m_lines(t1), _m_lines(t2)
    local = _slot(l1, 1, 1)
    if not local:
        raise Skip("W1 hosts no local Maestro 1")
    if _slot(l1, 1, 2) or _slot(l2, 1, 2):
        raise Skip("W2 already hosts a Maestro 1, or W1 already proxies one there")
    if len(l1) >= 9 or len(l2) >= 9:
        raise Skip(f"no free Maestro slot (W1 {len(l1)}, W2 {len(l2)} of 9)")
    port = _free_port(t2, 2, t1)
    if not port:
        raise Skip("W2 has no free port at 9600 among S2-S5 for the placeholder Maestro")
    local_line = f"?MAESTRO,M1:W1S{local[0]}:{local[1]}"
    bad, placed = [], False
    with config_guard(bench, 1, 2):
        try:
            out = [x.rstrip() for x in w2.run(f"?MAESTRO,M1:W2{port}:19200")]
            placed = _has(out, f"✓ Maestro 1: Local {port} at 19200 baud (slot ")
            if not placed:
                raise AssertionError(f"W2 did not take a local Maestro 1 on {port}: {out}")
            after2 = snapshot(bench, 2)
            for tok in (f"?MAESTRO,M1:W2{port}:19200", f"?BAUD,{port},19200", f"?BCAST,OUT,{port},OFF",
                        f"?BCAST,IN,{port},OFF"):
                if tok not in after2:
                    bad.append(f"W2's chain lacks {tok} with a Maestro on {port}")
            got, polls = _poll_expect(w, AUTO_ADDED.format(mid=1, host=2))
            if got is None:
                bad.append(f"W1 added no proxy for W2's Maestro 1 beside its own local Maestro 1 ({polls} polls)")
            elif int(got.group(1)) != 19200:
                bad.append(f"the proxy for W2's Maestro 1 was added at {got.group(1)} baud, not 19200")
            now1 = _m_lines(snapshot(bench, 1))
            if local_line.upper() not in [t.upper() for t in now1]:
                bad.append("W1's local Maestro 1 slot changed")
            if not _slot(now1, 1, 2):
                bad.append("W1's chain has no M1:W2 proxy")
        finally:
            if placed:
                out = [x.rstrip() for x in w2.run(f"?MAESTRO,CLEAR,M1:W2{port}")]
                if not _has(out, f"Cleared Maestro M1:W2{port} (freed slot "):
                    bad.append(f"W2 did not clear its Maestro 1: {out}")
                time.sleep(2.0)                   # W2's on-change advert no longer lists Maestro 1
                w.run("?WDP,POLL")
                time.sleep(2.0)
            proxy = _slot(_m_lines(snapshot(bench, 1)), 1, 2)
            if placed and not proxy and not bad:
                bad.append("W1 dropped the M1:W2 proxy by itself once W2 stopped hosting Maestro 1 - CLAUDE.md rule 5 "
                           "says proxies are never evicted: update it")
            if proxy:
                w.run(f"?MAESTRO,CLEAR,M1:W2S{proxy[0]}")
                w.run("?WDP,POLL")
                time.sleep(2.0)
                if _slot(_m_lines(snapshot(bench, 1)), 1, 2):
                    bad.append("W1 learnt the M1:W2 proxy again after W2 stopped hosting Maestro 1")
    assert not bad, "; ".join(bad)


# Rows 2 and 3: the RC relay. JSON from the mesh reaches W1's USB only while a host holds the 20 s window open
# (WCB.ino:7941-7943) and no OTA command came in the last 8 s (:399-402, :5454, :5762, :6600, :6612); it goes through
# a 64-slot queue drained in loop() (:377-411), and a full queue counts and prints every line it drops (:420-439).
@test("navicore.rc_relay_ota_pause", "Any ?OTALOCAL command pauses W1's RC telemetry relay for 8 s: a PING sent right after ?OTALOCAL,STATUS gets no relayed PONG and no rc_* line for 6 s, and a PING 9 s after it brings the PONG back (no OTA session, nothing erased; ~20 s)", needs=["wcb1", "navicore"], links=[])
def rc_relay_ota_pause(bench):
    """WCB-WP28 row 2 (wcb.rx.rc_relay_paused_during_ota, wcb.rcrelay.ota_pause). ?OTALOCAL sets
    otaRelayForwardUntilMs 8 s ahead before it does anything else (WCB.ino:6596-6602; ?OTA the same, :6608-6614), and
    both receive paths skip the relay while it runs (:5454, :5762): the JSON is consumed, not queued. STATUS opens no
    session and erases nothing (ota.status_local runs it with no opt-in). The ;W20,{json} PINGs keep the 20 s window
    open throughout. A line queued just before the pause can still drain just after it, so the silent window starts
    0.3 s after the STATUS's echo."""
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?CONTROLLER,ON,20")
    m = w.dev.mark()
    w.send(';W20,{"type":"PING"}')
    w.dev.expect(r'^\{"sys":1,"type":"PONG"', timeout=4, since=m)       # the window is open and relaying
    out = w.run("?OTALOCAL,STATUS")
    t0 = time.monotonic()
    if not _has(out, "---------- OTA Status ----------"):
        raise AssertionError(f"?OTALOCAL,STATUS printed no status block: {out[:3]}")
    time.sleep(0.3)
    m = w.dev.mark()
    w.send(';W20,{"type":"PING"}')
    time.sleep(6.0)
    held = [x for x in w.dev.since(m) if x.startswith('{"sys":1')]
    time.sleep(max(0.0, t0 + 9.0 - time.monotonic()))
    m = w.dev.mark()
    w.send(';W20,{"type":"PING"}')
    try:
        w.dev.expect(r'^\{"sys":1,"type":"PONG"', timeout=4, since=m)
        back = True
    except AssertionError:
        back = False
    assert not held, f"{len(held)} line(s) relayed within 6 s of ?OTALOCAL: {[x[:50] for x in held[:2]]}"
    assert back, "no relayed PONG 9 s after ?OTALOCAL: the relay did not resume"


RELAY_DROP = re.compile(r"\[RCBRG\] relay queue FULL \S+ dropped a JSON line \(total drops=(\d+)\)")


@test("client_mesh.rc_relay_drops_counted", "With W1's RC relay window open, a mesh client's JSON flood (~150 Hz for 3 s) that lands while W1's loop prints ?HELP, ?backup and ?config overflows the 64-slot relay queue, and every drop prints '[RCBRG] relay queue FULL ... (total drops=N)' with N counting up by one: no relayed line twice, and relayed plus dropped account for what was sent, less a little air loss (~20 s)", needs=["wcb1", "probe2"], links=[])
def rc_relay_drops_counted(bench):
    """WCB-WP28 row 3 (wcb.rcrelay.queue_full_visible). enqueueRcJsonRelay increments rcJsonRelayDrops under a mux
    and prints the running total on every drop (WCB.ino:420-439), so the printed totals must be consecutive: a drop
    with no line would skip a number. The drop line prints on the WiFi task - deliberate, and a rule-11 exposure only
    under a flood like this one (the plan's 'checked, not defects'). The relay prints each line as it arrived: the
    '{"sys":1' tag the plan expected is NaviCore's own, not the relay's (drainRcJsonRelay, :405-411), so the client's
    lines are matched on their own tag, anywhere in a console line (a WiFi-task line can glue onto a loop line,
    HIL_TESTING.md §6). NaviCore's telemetry shares the queue and its drops count too, and an unensured frame can
    be lost in the air, so the accounting allows the drop count plus a tenth. Unensured JSON never enters a
    duplicate ring, so the client shares the JSON test's id (s19 MESH_IDS). The window needs the controller."""
    require_tokens(bench, 1, "?CONTROLLER,ON,20")
    w = usb_wcb(bench)
    tag = nonce()
    rx = re.compile(rf'\{{"hil":"{tag}","k":(\d+)\}}')
    with _client(bench, "probe2", "json_flood") as (probe, cid, _):
        w.send(';W20,{"type":"PING"}')         # opens the 20 s window whether or not NaviCore answers
        time.sleep(1.0)
        wm = w.dev.mark()
        for cmd in ("?HELP", "?backup", "?config"):
            w.send(cmd)
        sent, k, end = [], 0, time.monotonic() + 3.0
        while time.monotonic() < end:
            if probe.mesh_broadcast(f'{{"hil":"{tag}","k":{k}}}', ensured=False):
                sent.append(k)
            k += 1
            time.sleep(0.005)
        w.run("?PEERSLIVE", timeout=30)
        time.sleep(2.0)
        lines = w.dev.since(wm)
        rebooted = w.rebooted_since(wm)
    relayed = [int(x) for line in lines for x in rx.findall(line)]
    totals = [int(m.group(1)) for line in lines for m in RELAY_DROP.finditer(line)]
    missing = len(set(sent) - set(relayed))
    allowed = len(totals) + max(5, len(sent) // 10)
    bench.note(f"RC relay flood: {len(sent)} sent, {len(set(relayed))} relayed, {len(totals)} drops reported "
               f"(totals {totals[:1]}..{totals[-1:]}), {missing} not relayed")
    assert not rebooted, "W1 rebooted under the flood"
    assert len(relayed) == len(set(relayed)), "a JSON line was relayed twice"
    assert set(relayed) <= set(sent), "a relayed line was never sent"
    if not totals:
        raise Skip(f"the flood never filled W1's 64-slot relay queue ({len(set(relayed))} of {len(sent)} relayed): "
                   f"no drop to count")
    assert totals == list(range(totals[0], totals[0] + len(totals))), f"the drop totals skip a number: {totals}"
    assert missing <= allowed, (f"{missing} of {len(sent)} lines were neither relayed nor counted: {len(totals)} drops "
                                f"reported, {allowed} allowed with air loss")


# Row 4: a PWM output keeps its provenance in NVS: the prior broadcast flags a clear restores ('pbo<n>', 'pbi<n>')
# and the WDP source that auto-configured it ('auto<n>', 0 = manual), saved with each port (WCB_PWM.cpp:1055-1067)
# and loaded at boot (:1069-1107). Self-heal clears only an output whose tag is the advertising board
# (reconcileWdpAutoPWMOutputs, :995-1022). s25's pwm.wdp_selfheal_missed_clear is the flow the two tests below extend.
@test("pwm.output_prior_flags_survive_reboot", "A PWM output port's prior broadcast flags survive a reboot: S4 set OFF/OFF, declared a PWM output, W1 rebooted, then cleared - the chain holds ?BCAST,OUT,S4,OFF and ?BCAST,IN,S4,OFF again, not the ON the load's defaults would give (2 reboots)", needs=["wcb1"], links=[])
def output_prior_flags_survive_reboot(bench):
    """WCB-WP28 row 4, arm 1 (pwm.output_flags_and_provenance_persist). addPWMOutputPort records the port's flags as
    it claims it (WCB_PWM.cpp:984-986) and saves them; removePWMOutputPort puts them back (:1041-1051).
    pwm.clear_out_restores_flags clears before any reboot, so it reads the flags from RAM; here the clear comes after
    a reboot, so they must come back from NVS, whose defaults (pbo true, pbi false, :1091-1092) would turn both on."""
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON")
    bad = []
    with config_guard(bench, 1):
        declared = False
        try:
            w.run("?BCAST,OUT,S4,OFF")
            w.run("?BCAST,IN,S4,OFF")
            if not _has(w.run("?MAP,PWM,OUT,S4"), "Serial4 configured as PWM output port"):
                raise AssertionError("?MAP,PWM,OUT,S4 did not confirm")
            declared = True
            w.reboot()
            if not _has(w.run("?MAP,PWM,LIST"), "Configured outputs: S4"):
                bad.append("S4 is not a PWM output after the reboot")
            _inline_clear_out(w, "S4")
            declared = False
            toks = snapshot(bench, 1)
            flags = [t for t in toks if t.upper().startswith(("?BCAST,OUT,S4,", "?BCAST,IN,S4,"))]
            if "?BCAST,OUT,S4,OFF" not in toks or "?BCAST,IN,S4,OFF" not in toks:
                bad.append(f"after the reboot and the clear S4's flags are {flags}, expected both OFF")
        finally:
            if declared or "?MAP,PWM,OUT,S4" in snapshot(bench, 1):
                _inline_clear_out(w, "S4")
            w.run("?BCAST,OUT,S4,ON")
            w.run("?BCAST,IN,S4,ON")
    assert not bad, "; ".join(bad)


def _auto_configure_w2_s3(bench, w, w2):
    """The first half of pwm.wdp_selfheal_missed_clear: W1 maps S3 -> W2S3 (a W1 reboot), which declares W2's S3 by an
    explicit command (a MANUAL output, tag 0); a direct clear takes it away (a W2 reboot); ?WDP,POLL then has W2
    auto-configure it from W1's advert, tagged with W1 as its source. Returns W2's console mark before the poll."""
    m = w.send("?MAP,PWM,S3,W2S3")
    _pwm_reboot(w, m)
    if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
        raise AssertionError("setup: W2 did not take the output")
    time.sleep(5.0)              # W1's boot burst of adverts would re-auto-configure W2's S3 in its quiet window
    for _ in range(3):
        _clear_remote_out(w, "S3")
        if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
            break
    if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
        raise AssertionError("setup: W2 still declares S3 after the direct clear")
    cm = w2.dev.mark()
    if not _has(w.run("?WDP,POLL"), "[WDP] polled"):
        raise AssertionError("?WDP,POLL did not confirm")
    w2.dev.expect(r"\[WDP\] WCB1 drives our S3 .* auto-configuring PWM output", timeout=8, since=cm)
    time.sleep(2.0)
    if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
        raise AssertionError("setup: WDP auto-config did not declare W2 S3")
    return cm


def _clear_all_while_deaf(bench, w, bad):
    """W2 deaf (hil.ncmesh.deaf, its own console): W1's ?MAP,PWM,CLEAR,ALL and its reboot, whose explicit clear to W2
    is not heard. Returns W2's console mark taken before W2 can hear again: W1's boot burst of adverts may heal W2 the
    moment its octet comes back (pwm.wdp_selfheal_missed_clear, run 20260924-190733). W1 must see W2 online first: a
    peer is offline to a W1 that has just rebooted until its next packet, and a unicast to an offline peer goes out
    once, untracked, so no 'failed to ACK' would ever come (pwm.wdp_selfheal_spares_manual_output, run
    20260928-220200)."""
    _w2_online(bench, w)
    with deafened(bench, 2) as w2:
        w.run("?DEBUG,ETM,ON")
        wm = w.dev.mark()
        m = w.send("?MAP,PWM,CLEAR,ALL")
        w.dev.expect(r"All PWM mappings cleared", timeout=3, since=m)
        try:
            w.dev.expect(r"\[ETM\] WCB2 failed to ACK seq \d+ after 3 retries: \?MAP,PWM,CLEAR,OUT,S3", timeout=8, since=wm)
        except AssertionError:
            bad.append("W1's explicit clear to W2 was not seen to fail (W2 not deaf?)")
        _pwm_reboot(w, m)
        w.run("?DEBUG,ETM,OFF")
        if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
            bad.append("W2 lost the output while deaf: the explicit clear got through")
        return w2.dev.mark()


def _undo_w2_s3(bench, w, w2, s3):
    """The shared cleanup: W1's input held low, any W1 mapping cleared, any W2 S3 output cleared on W2's own console
    (a deferred reboot there), the probe's pins released."""
    s3.pwm_out(0)
    if any(t.startswith("?MAP,PWM") for t in snapshot(bench, 1)):
        _clear_local_mapping(w, "S3")
    if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
        _inline_clear_out(w2, "S3")
        _w2_online(bench, w)
    s3.pwm_stop()


@test("pwm.wdp_autotag_survives_receiver_reboot", "A WDP-auto-configured PWM output on W2 keeps its source tag across a W2 reboot: after the reboot, W2 missing W1's explicit clear (deaf) still clears the output by self-heal at W1's next advert, without rebooting (W1 x2, W2 x2 reboots; W2 deaf for a few seconds)", needs=["wcb1", "wcb2"], links=["W1S3"])
def wdp_autotag_survives_receiver_reboot(bench):
    """WCB-WP28 row 4, arm 2 (wdp.pwm.autotag_persists_reboot). pwm.wdp_selfheal_missed_clear with a W2 reboot between
    the auto-config and the missed clear: the tag must come back from W2's NVS ('auto<n>', WCB_PWM.cpp:1060-1062,
    :1084-1090). Loaded as 0 (a manual output) it would never be self-healed. After the reboot W1's adverts still name
    the port, and an existing output is never re-tagged (addPWMOutputPort, :973-977), so only the saved tag can make
    the heal happen."""
    s3 = link(bench, 1, "S3")
    w, w2 = usb_wcb(bench), _w2(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    _wdp_gates(bench, 1, 2)
    bad = []
    with config_guard(bench, 1, 2):
        try:
            s3.pwm_out(0)
            _auto_configure_w2_s3(bench, w, w2)
            w2.reboot()
            _w2_online(bench, w)
            if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
                raise AssertionError("W2 lost the auto-configured output across its reboot")
            cm = _clear_all_while_deaf(bench, w, bad)
            if not _has(w.run("?WDP,POLL"), "[WDP] polled"):
                bad.append("?WDP,POLL did not confirm")
            try:
                w2.dev.expect(r"\[WDP\] WCB1 no longer drives our S3 - clearing auto-configured PWM output",
                              timeout=10, since=cm)
            except AssertionError:
                bad.append("W2 did not self-heal the output it auto-configured before its reboot: its source tag did "
                           "not survive the reboot")
            time.sleep(2.0)
            if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                bad.append("W2 still declares S3 after the self-heal")
            if w2.rebooted_since(cm):
                bad.append("the self-heal rebooted W2 (it must clear live)")
        finally:
            _undo_w2_s3(bench, w, w2, s3)
    assert not bad, "; ".join(bad)


@test("pwm.wdp_selfheal_spares_manual_output", "A PWM output W2 declared by hand is never cleared by WDP self-heal: W1 maps S3 to it, W2 misses W1's explicit clear (deaf), and W1's later adverts without the target leave W2's manual output in place (W1 x2, W2 x1 reboots; W2 deaf for a few seconds)", needs=["wcb1", "wcb2"], links=["W1S3"])
def wdp_selfheal_spares_manual_output(bench):
    """WCB-WP28 row 4, arm 3. W2's own ?MAP,PWM,OUT,S3 tags the port manual (0); W1's explicit remote config and its
    WDP adverts then meet an existing output and never re-tag it (addPWMOutputPort, WCB_PWM.cpp:973-977;
    WCB_WDP.cpp:766). When W1's adverts stop naming the port, reconcileWdpAutoPWMOutputs clears only outputs tagged
    with W1 (WCB_PWM.cpp:1004-1016), so the manual one stays. W2 must be deaf for W1's CLEAR,ALL, or its explicit
    ?MAP,PWM,CLEAR,OUT would remove the port whatever its tag."""
    s3 = link(bench, 1, "S3")
    w, w2 = usb_wcb(bench), _w2(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    _wdp_gates(bench, 1, 2)
    bad = []
    with config_guard(bench, 1, 2):
        try:
            if not _has(w2.run("?MAP,PWM,OUT,S3"), "Serial3 configured as PWM output port"):
                raise AssertionError("W2 did not take the manual PWM output on S3")
            s3.pwm_out(0)
            cm = w2.dev.mark()
            m = w.send("?MAP,PWM,S3,W2S3")
            _pwm_reboot(w, m)
            time.sleep(3.0)
            if _has(w2.dev.since(cm), "auto-configuring PWM output"):
                bad.append("W2 re-tagged its manual S3 output from W1's advert")
            cm = _clear_all_while_deaf(bench, w, bad)
            w.run("?WDP,POLL")
            time.sleep(6.0)
            if _has(w2.dev.since(cm), "no longer drives our S3"):
                bad.append("WDP self-heal cleared W2's manual S3 output")
            if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
                bad.append("W2 no longer declares its manual S3 output")
        finally:
            _undo_w2_s3(bench, w, w2, s3)
    assert not bad, "; ".join(bad)
