"""Boot-time restore per subsystem (docs/HIL_TEST_AUDIT.md WP2): a setting saved to NVS is what the board runs after a
reboot - checked on the boot banner, in the config chain, and in behaviour (bytes on a wire, a refused command).

Labels, ETM settings, serial and PWM mappings, variables and the Kyber modes already have reboot tests (s06, s13,
s14, s16, s18, s22). This suite covers the rest: the HCR / MP3 / DFPlayer host configs and W1's learned routes, a
local WLED, the alias, explicit ?BCAST flags, the delimiter, WDP off, WCBQ, and a remote Maestro proxy.

The coverage re-scan (docs/hil_plan/WCB.md) added the boot sequence itself (WCB-WP36: one boot per ?reboot and its
time against the boot guard, the ETM boot announces and heartbeat window, the WDP boot burst, the advert phase by board
number) and the non-default settings still without a reboot test (WCB-WP47: the command character, the function
identifier, auto-join off, S0 echo, ETM MISS/BOOT/COUNT/DELAY, the RAM-only debug flags, the controller OFF, and the
MP3/DFPlayer ONERR keys and current volumes).

Every test restores in a finally and runs under config_guard (the WP36 timing tests change nothing); W1 reboots 14 times
in all (~12 s each) and W2 four times. Nothing here moves a servo. A restore if aborted, on W1's USB console:
<function identifier>FUNCCHAR,? and ?CMDCHAR,; if either was left changed (chars.*_reboot), ?WDP,AUTOJOIN,ON,
?BCAST,OUT,S0,OFF, the ?ETM lines of W1's chain, ?CONTROLLER,ON,20; on W2's: ?MP3,CLEAR, ?DFP,CLEAR, ?SEQ,CLEAR,HILE
and its baseline labels; then s15's _unlearn order for W1's learned MP3/DFP routes.
"""
import re
import time

from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import Console, Watch, config_guard, link, marker, nonce, require_tokens, snapshot, token, usb_wcb
from suites.s15_hcr_mp3_dfp import (_clear_all_w2, _device_tokens, _dfp, _in_order, _inject, _lf, _no_recall_keys,
                                     _relabel, _require_free, _run, _steps, _unlearn)
from suites.s18_etm_config_wdp import _prefix_chars, _restore_prefixes, _sent
from suites.s24_wled_config import ON as WLED_ON, _ids_free, _wdp_off
from suites.s24_wled_config import _relabel as _relabel_w      # the WCB (not Console) flavour
from suites.s99_etm import _peers_online


def _has(lines, text):
    return any(text in x for x in lines)


def _reboot_w2(w):
    m = w.send(";W2,?reboot")
    w.dev.expect(r"^\[ETM\] WCB2 came ONLINE \(boot\)", timeout=30, since=m)
    time.sleep(3)
    return m


@test("boot.devices_restore_w2", "MP3 on S3, DFPlayer on S5 and HCR on S4 configured on W2 come back after a W2 reboot: the [..] Loaded lines, an identical config chain, and each device's bytes; W1's learned routes survive (W2 reboot)", needs=["wcb1"], links=["W2S3", "W2S4", "W2S5"])
def devices_restore_w2(bench):
    """loadHCRSettings / loadMP3Settings / loadDFPSettings print '[HCR] Loaded: S4 at 9600 baud, poll=0s' and the
    '[MP3] Loaded: S3 at ...' / '[DFP] Loaded: S5 at ...' lines at boot (WCB_HCR.cpp:1007, WCB_MP3.cpp:462,
    WCB_DFP.cpp:413). W2's own USB console captures its boot output."""
    s3, s4, s5 = link(bench, 2, "S3"), link(bench, 2, "S4"), link(bench, 2, "S5")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    for p in ("S3", "S4", "S5"):
        _require_free(bench, 2, p)
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has device config")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its boot lines cannot be read")
        try:
            for cmd in ("?MP3,S3:9600:V64", "?DFP,S5:9600:V0", "?HCR,POLL,OFF", "?HCR,PORT,S4:9600"):
                _run(c2, cmd, 1.0)
            time.sleep(2)
            configured = snapshot(bench, 2)
            routes = [t for t in snapshot(bench, 1) if t.upper().startswith(("?MP3,REMOTE", "?DFP,REMOTE", "?HCR,REMOTE"))]
            bm = c2.mark()
            _reboot_w2(w)
            boot = c2.lines(bm)
            problems += [f"boot lacks {x!r}" for x in ("[HCR] Loaded: S4 at 9600 baud, poll=0s",
                                                       "[MP3] Loaded: S3 at 9600 baud  default vol=64  current vol=64",
                                                       "[DFP] Loaded: S5 at 9600 baud  default vol=0  current vol=0") if not _has(boot, x)]
            after = snapshot(bench, 2)
            if after != configured:
                problems.append(f"W2's chain changed across the reboot: missing {[t for t in configured if t not in after]} / extra {[t for t in after if t not in configured]}")
            if [t for t in snapshot(bench, 1) if t.upper().startswith(("?MP3,REMOTE", "?DFP,REMOTE", "?HCR,REMOTE"))] != routes:
                problems.append("W1's routes changed while W2 rebooted")
            problems += _steps(s4, w.send, [(";H,OVERLOAD", _lf("<SE,QT>"))])
            problems += _steps(s3, w.send, [(";A,PLAY,5", bytes.fromhex("76407405"))])
            problems += _steps(s5, w.send, [(";D,PLAY,5", _dfp(0x03, 5))])
        finally:
            _clear_all_w2(c2)
            _relabel(c2, before[2], "S3", "S4", "S5")
            _unlearn(bench, "MP3", learner=1)
            _unlearn(bench, "DFP", learner=1)
    assert not problems, "; ".join(problems)


@test("boot.wled_local_restore", "A local WLED on W1 S4 is loaded at boot ('[WLED] Loaded: WLED 3 -> local S4 @ 9600 baud'), its port stays reserved and ;L bytes flow (WDP off; 1 reboot)", needs=["wcb1"])
def wled_local_restore(bench):
    s4 = link(bench, 1, "S4")
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    from suites.s24_wled_config import _require_free as _wled_free
    _wled_free(bench, 1, "S4")
    _ids_free(bench, 3)
    problems = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            try:
                assert _has(w.run(f"?WLED,3:W{me}S4:9600"), "[WLED] WLED 3: local S4 at 9600 baud (slot ")
                bm = w.reboot()
                boot = w.dev.since(bm)
                if not _has(boot, "[WLED] Loaded: WLED 3 -> local S4 @ 9600 baud"):
                    problems.append("boot lacks the WLED Loaded line")
                toks = snapshot(bench, 1)
                problems += [f"after the reboot the chain lacks {t}" for t in (f"?WLED,3:W{me}S4:9600", "?BCAST,OUT,S4,OFF", "?LABEL,S4,WLED 3") if t not in toks]
                watch = Watch(s4)
                w.send(";L3,ON")
                try:
                    watch.expect(s4, WLED_ON, timeout=2)
                except AssertionError:
                    problems.append(f";L3,ON after the reboot did not reach W1 S4: {watch.got(s4)!r}")
            finally:
                w.run("?WLED,CLEAR,3")
                _relabel_w(w, before[1], "S4")
    assert not problems, "; ".join(problems)


@test("persist.alias_reboot", "?ALIAS survives a W1 reboot: ?ALIAS reports it, the chain holds it, and W2 routes ;W<alias> to W1 afterwards (1 reboot)", needs=["wcb1"], links=[])
def alias_reboot(bench):
    w = usb_wcb(bench)
    name = f"HIL{marker()[3:9]}"
    problems = []
    with config_guard(bench, 1) as before:
        orig = token(before[1], "?ALIAS,")
        try:
            assert _has(w.run(f"?ALIAS,{name}"), f"WCB alias set to: {name}")
            w.reboot()
            if not _has(w.run("?ALIAS"), f"Alias: {name}"):
                problems.append("?ALIAS after the reboot does not report the alias")
            if f"?ALIAS,{name}" not in snapshot(bench, 1):
                problems.append("the chain lost the alias across the reboot")
            time.sleep(3)                                   # W1's boot adverts carry the alias to W2
            t = marker()
            m = w.dev.mark()
            w.send(f"?MGMT,FRAG,2,{marker()[3:7]},0,1,;W{name},;S0{t}")
            try:
                w.dev.expect(rf"^{t}$", timeout=5, since=m)
            except AssertionError:
                problems.append("W2 did not resolve the alias to W1 after W1's reboot")
        finally:
            w.run(orig if orig else "?ALIAS,CLEAR")
            time.sleep(2)
    assert not problems, "; ".join(problems)


@test("persist.bcast_flags_reboot", "Explicit ?BCAST,OUT,S4,OFF and ?BCAST,IN,S5,OFF survive a W1 reboot in the chain and in behaviour (1 reboot)", needs=["wcb1"])
def bcast_flags_reboot(bench):
    s3, s4, s5 = link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, "?BCAST,OUT,S3,ON", "?BCAST,OUT,S4,ON", "?BCAST,IN,S5,ON")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1):
        try:
            w.run("?BCAST,OUT,S4,OFF")
            w.run("?BCAST,IN,S5,OFF")
            w.reboot()
            toks = snapshot(bench, 1)
            problems += [f"after the reboot the chain lacks {t}" for t in ("?BCAST,OUT,S4,OFF", "?BCAST,IN,S5,OFF") if t not in toks]
            t = marker()
            watch = Watch(s3, s4)
            w.send(t)
            try:
                watch.expect(s3, t.encode() + b"\r", timeout=2)
            except AssertionError:
                problems.append("a broadcast did not reach S3 after the reboot")
            time.sleep(1.0)
            if t.encode() in watch.got(s4):
                problems.append("S4's saved OUT,OFF did not hold after the reboot")
            s5.send(b"\r")
            time.sleep(0.3)
            m = w.dev.mark()
            s5.send(marker().encode() + b"\r")
            try:
                w.dev.expect(r"^Broadcast blocked from Serial5 \(input blocking enabled\)", timeout=2, since=m)
            except AssertionError:
                problems.append("S5's saved IN,OFF did not hold after the reboot")
        finally:
            w.run("?BCAST,OUT,S4,ON")
            w.run("?BCAST,IN,S5,ON")
    assert not problems, "; ".join(problems)


@test("chars.delim_reboot", "?DELIM,| survives a W1 reboot (?config shows it, a | chain runs both halves); restored before any chain is compared (1 reboot)", needs=["wcb1"], links=[])
def delim_reboot(bench):
    """The harness splits chains on '^', so the delimiter goes back before config_guard's after-snapshot; the check after
    the boot uses ?config and a ;S0 chain only."""
    w = usb_wcb(bench)
    t = marker()
    problems = []
    with config_guard(bench, 1):
        try:
            m = w.dev.mark()
            w.dev.send("?DELIM,|")
            w.dev.expect(r"^Delimiter updated to: '\|'", timeout=3, since=m)
            w.reboot()
            m = w.dev.mark()
            w.dev.send("?config")
            w.dev.expect(r"Delimiter Character:\s+\|", timeout=5, since=m)
            m = w.dev.mark()
            w.dev.send(f";S0A{t}|;S0B{t}")
            w.dev.expect(rf"^B{t}$", timeout=3, since=m)
            if f"A{t}" not in [x.strip() for x in w.dev.since(m)]:
                problems.append("after the reboot a | chain did not run both halves")
        finally:
            m = w.dev.mark()
            w.dev.send("?DELIM,^")
            try:
                w.dev.expect(r"^Delimiter updated to: '\^'", timeout=3, since=m)
            except AssertionError:
                w.dev.send("?D^")
                time.sleep(0.5)
    assert not problems, "; ".join(problems)


@test("wdp.off_reboot", "?WDP,OFF survives a W1 reboot: WDPCFG EN=0 after the boot, POLL refused, no adverts sent; ON restores (1 reboot)", needs=["wcb1"], links=[])
def off_reboot(bench):
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        if "?WDP,OFF" in before[1]:
            raise Skip("W1's WDP is already off")
        try:
            assert _has(w.run("?WDP,OFF"), "[WDP] disabled")
            w.reboot()
            dump = w.run("?WDP,DUMP", timeout=8)
            if not _has(dump, "[WDPCFG:EN=0,"):
                problems.append(f"after the reboot WDPCFG is not EN=0: {[x for x in dump if x.startswith('[WDPCFG')]}")
            if not _has(w.run("?WDP,POLL"), "[WDP] POLL needs WDP + ETM enabled"):
                problems.append("POLL was not refused after the reboot")
            assert _has(w.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled")
            m = w.dev.mark()
            time.sleep(8)
            if [x for x in w.dev.since(m) if x.startswith("[WDP] advert sent")]:
                problems.append("W1 advertised with WDP off after the reboot")
        finally:
            w.run("?DEBUG,MGMT,OFF")
            on = w.run("?WDP,ON")
            w.run("?WDP,POLL")
            if not _has(on, "[WDP] enabled"):
                problems.append("?WDP,ON did not confirm")
    assert not problems, "; ".join(problems)


@test("peers.wcbq_reboot", "?WCBQ,3 survives a W1 reboot: the banner and ?config count 3, WCB3 is registered but never seen; ?WCBQ,2 puts it back live (1 reboot)", needs=["wcb1"], links=[])
def wcbq_reboot(bench):
    """Never below the real fleet (2): W2 would auto-join as a persisted learned peer on its next advert."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        if token(before[1], "?WCBQ,") != "?WCBQ,2":
            raise Skip("W1's WCBQ is not 2")
        try:
            assert _has(w.run("?WCBQ,3"), "Saved WCB quantity: 3.")
            bm = w.reboot()
            if not _has(w.dev.since(bm), "Number of WCBs in the system: 3"):
                problems.append("the boot banner does not count 3 WCBs")
            cfg = w.run("?config")
            if not _has(cfg, "Number of WCBs in the system: 3") or not any(re.match(r"^  WCB3: 02:[0-9A-F]{2}:[0-9A-F]{2}:00:00:03  Not yet seen", x) for x in cfg):
                problems.append("?config after the reboot does not list WCB3 as not yet seen")
            if not any(x.startswith("Live peers: ") and "(WCBQ floor 3" in x for x in w.run("?PEERSLIVE")):
                problems.append("?PEERSLIVE after the reboot does not show floor 3")
        finally:
            w.run("?WCBQ,2")
        if not any(x.startswith("Live peers: ") and "(WCBQ floor 2" in x for x in w.run("?PEERSLIVE")):
            problems.append("?WCBQ,2 did not restore the floor live")
    assert not problems, "; ".join(problems)


@test("boot.maestro_proxy_restore", "A remote Maestro proxy added on W1 is loaded at boot and listed after the reboot; cleared afterwards (WDP off; 1 reboot)", needs=["wcb1"], links=[])
def maestro_proxy_restore(bench):
    """Remote proxies are never advertised (only local slots are, WCB_WDP.cpp), so adding one with WDP off changes
    nothing on W2 or NaviCore. Host W11 is a placeholder no board answers to."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        lines = [t for t in before[1] if re.match(r"^\?MAESTRO,M\d:W\d+S\d:\d+$", t, re.I)]
        if len(lines) > 8 or any(t.upper().startswith("?MAESTRO,M8:") for t in lines):
            raise Skip("W1 needs a free Maestro slot and no Maestro 8")
        with _wdp_off(w, before[1]):
            try:
                assert _has(w.run("?MAESTRO,M8:W11S1:9600"), "✓ Maestro 8: Remote on WCB11 (unicast, slot ")
                bm = w.reboot()
                boot = w.dev.since(bm)
                if not any(re.search(r"Maestro 8 → WCB11", x) for x in boot):
                    problems.append("the boot output does not list the proxy for Maestro 8")
                lst = [x.rstrip() for x in w.run("?MAESTRO,LIST")]
                if "  Maestro 8 → WCB11" not in lst:
                    problems.append(f"?MAESTRO,LIST after the reboot lacks Maestro 8: {lst}")
                if "?MAESTRO,M8:W11S1:9600" not in snapshot(bench, 1):
                    problems.append("the chain lost the proxy across the reboot")
            finally:
                w.run("?MAESTRO,CLEAR,M8")
    assert not problems, "; ".join(problems)


# ============================================================ the boot sequence (WCB-WP36)
FORWARDING = "Raw Serial Forwarding Task Created"   # setup()'s last line (WCB.ino:9748)
BOOT_LIMIT_S = 7.0      # against the 10 s boot guard (BOOT_GUARD_TIMEOUT_MS, WCB.ino:8913); the bench boots in 2.7-3.3 s


def _t(pairs, pattern, after=None):
    """Host time of the first (time, line) pair matching `pattern`, at or after time `after`; None when there is none."""
    rx = re.compile(pattern)
    return next((t for t, x in pairs if (after is None or t >= after) and rx.search(x)), None)


def _boot_record(pairs):
    """What one ?reboot's output says about the boot(s) in it, from (host time, line) pairs: how many ROM 'rst:' lines and
    forwarding-task lines, the 'Boot attempts since power applied: <n>' counts and the reset reasons (printResetReason,
    WCB.ino:8611-8640), and the seconds from the first rst: to the first forwarding-task line. Only these lines are
    read, and none is quoted whole: a boot also prints the mesh password and the access point's name."""
    rst = [t for t, x in pairs if x.startswith("rst:")]
    done = [t for t, x in pairs if x.rstrip() == FORWARDING]
    return {"rst": len(rst), "done": len(done),
            "attempts": [int(m.group(1)) for _, x in pairs
                         for m in [re.match(r"^Boot attempts since power applied: (\d+)", x)] if m],
            "reasons": [x.rstrip() for _, x in pairs if x.startswith("Reset reason: ")],
            "secs": round(done[0] - rst[0], 2) if rst and done and done[0] > rst[0] else None}


def _after_boot(dev, mark):
    """The lines a board printed after one boot's forwarding-task line, the boot being the first after `mark`."""
    lines = [x.rstrip() for x in dev.since(mark)]
    i = next((k for k, x in enumerate(lines) if x == FORWARDING), None)
    return [] if i is None else lines[i + 1:]


@test("boot.attempts_and_timing", "Two W1 reboots and one W2 reboot each boot exactly once - one rst:, one 'Raw Serial Forwarding Task Created', a software reset - and W1's RTC boot-attempt counter goes up by exactly one per reboot (a boot-guard or panic restart on the way would add another); each boot reaches setup()'s end within 7 s of its rst:, against the 10 s boot guard (3 reboots)", needs=["wcb1", "wcb2"], links=[])
def attempts_and_timing(bench):
    """WCB-WP36 row 1. printResetReason counts boots in RTC noinit RAM (g_bootAttempts, WCB.ino:8606-8640), which a
    software, watchdog or panic reset keeps, so one ?reboot adds exactly one. The boot guard restarts a setup() still
    running 10 s after it began (BOOT_GUARD_TIMEOUT_MS, WCB.ino:8895-8933; armed first in setup() at :9293, disarmed at
    its end at :9761), and WCB.wait_boot passes a board that restarted once on the way: this is the test that notices.
    The comment at :8910-8912 wants a measured worst case before the 10 s is lowered, so every boot's time is noted. W1
    boots with its bench config, WiFi access point up; the plan's ~5 KB of extra sequences for a worst case is not
    stored first, on W1's fragmented NVS. Changes nothing."""
    w = usb_wcb(bench)
    problems, recs = [], []
    with Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its boot lines cannot be read")
        for _ in range(2):
            m = w.reboot()
            recs.append(("W1", _boot_record(list(w.dev.lines[m:]))))
        m = WCB(c2.dev).reboot()
        recs.append(("W2", _boot_record(list(c2.dev.lines[m:]))))
    bench.note("rst: to 'Raw Serial Forwarding Task Created': " + ", ".join(f"{n} {r['secs']} s" for n, r in recs)
               + f"; W1 boot attempts {[r['attempts'] for n, r in recs if n == 'W1']}")
    for name, r in recs:
        if r["rst"] != 1 or r["done"] != 1 or len(r["attempts"]) != 1:
            problems.append(f"{name} printed {r['rst']} rst: line(s), {r['done']} forwarding-task line(s) and boot "
                            f"attempts {r['attempts']} for one ?reboot")
        if r["reasons"] != ["Reset reason: 3 - Software Reset"]:
            problems.append(f"{name}'s reset reason(s): {r['reasons']}")
        if r["secs"] is None or r["secs"] > BOOT_LIMIT_S:
            problems.append(f"{name} took {r['secs']} s from rst: to the end of setup() (limit {BOOT_LIMIT_S} s)")
    a1, a2 = recs[0][1]["attempts"], recs[1][1]["attempts"]
    if len(a1) == 1 and len(a2) == 1 and a2[0] != a1[0] + 1:
        problems.append(f"W1's boot-attempt counter went {a1[0]} -> {a2[0]} across one ?reboot")
    _peers_online(bench, w, strict=False)
    assert not problems, "; ".join(problems)


@test("boot.announce_advert_burst", "After a W1 reboot, with no POLL anywhere: W1 sends exactly three ETM boot announces ~1.2 s apart, the first ~1.5 s after its ETM block, and its first heartbeat inside the ETM,BOOT window; W2 hears one to three of them, each a '(boot)' ONLINE edge; W1's three boot adverts leave W2's WDP row for W1 at most 2 s old within 6 s of W1's banner (1 reboot)", needs=["wcb1", "wcb2"], links=[])
def announce_advert_burst(bench):
    """WCB-WP36 rows 2 and 3. setup() schedules the boot heartbeat at a random 1 s to ETM,BOOT s (scheduleNextHeartbeat,
    WCB.ino:1339-1345) and three PACKET_TYPE_ETM_BOOT broadcasts from 1.5 s on, 1.2 s apart (:9699-9705, :1364-1373),
    printed under ?DEBUG,ETM as '[ETM] Boot announce sent (WCB<n>)' and '[ETM] Heartbeat sent (WCB<n>)' (:1286, :1268);
    '[ETM] Heartbeat scheduled (boot window)' marks that moment (:9712). A receiver prints '[ETM] WCB<n> came ONLINE
    (boot)' for each announce (:5281-5292), and '[ETM] Boot announce from WCB<n>' under ?DEBUG,ETM (:5312-5316).
    wdpBegin arms three boot adverts 1.3 s apart from 1.6 s after it runs (WCB_WDP.cpp:1965-1966, :378-382), and a DUMP
    row's AGE is the whole seconds since that neighbour's last advert (:1822-1826). W1's ?DEBUG,ETM is sent the moment
    its forwarding-task line arrives: its USB reader takes it 500 ms after starting (WCB.ino:8697), before the earliest
    heartbeat (1 s) and the first announce (1.5 s); should it land later, W2's receive side is used instead. Broadcasts
    are not acknowledged, so W2 may miss one: it must hear one to three, never more. Changes nothing."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    toks = bench.config_tokens(1, refresh=True)
    if "?ETM,ON" not in toks or "?WDP,OFF" in toks:
        raise Skip("W1 has ETM or WDP off")
    boot_s = int((token(toks, "?ETM,BOOT,") or "?ETM,BOOT,2").split(",")[2])
    hb_s = int((token(toks, "?ETM,HB,") or "?ETM,HB,10").split(",")[2])
    problems, notes = [], []
    with Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its ETM debug lines cannot be read")
        try:
            if not _has(_run(c2, "?DEBUG,ETM,ON"), "ETM debugging enabled"):
                problems.append("W2's ?DEBUG,ETM,ON did not confirm")
            cm = c2.mark()
            m = w.send("?reboot")
            w.dev.expect(r"^Reboot queued", timeout=3, since=m)
            w.dev.expect(r"^Rebooting now", timeout=w.REBOOT_DEFER_S, since=m)
            w.dev.expect(rf"^{FORWARDING}", timeout=30, since=m)
            w.dev.send("?DEBUG,ETM,ON")
            t_task = _t(list(w.dev.lines[m:]), rf"^{FORWARDING}")
            time.sleep(max(0.0, t_task + 4.0 - time.monotonic()))      # the banner is ~0.6 s before t_task
            dm = c2.send("?WDP,DUMP")
            c2.expect(r"\[WDP:END,", timeout=6, since=dm)
            dump = list(c2.dev.lines[dm:])
            time.sleep(max(0.0, t_task + max(6.0, boot_s + 1.5) - time.monotonic()))
            p1, p2 = list(w.dev.lines[m:]), list(c2.dev.lines[cm:])
        finally:
            _run(c2, "?DEBUG,ETM,OFF")
            w.run("?DEBUG,ETM,OFF")
    t_banner = _t(p1, r"^Booting up the ")
    t_sched = _t(p1, r"^\[ETM\] Heartbeat scheduled \(boot window\)")
    t_dbg = _t(p1, r"^ETM debugging enabled", t_task)
    if t_sched is None or t_dbg is None:
        raise AssertionError(f"W1's boot printed no '[ETM] Heartbeat scheduled (boot window)' ({t_sched is None}) or its "
                             f"?DEBUG,ETM,ON after the boot did not confirm ({t_dbg is None})")
    lead = t_dbg - t_sched
    sent = [t - t_sched for t, x in p1 if x.startswith(f"[ETM] Boot announce sent (WCB{me})")]
    beats = [t - t_sched for t, x in p1 if x.startswith(f"[ETM] Heartbeat sent (WCB{me})") and t > t_sched]
    heard = [t - t_sched for t, x in p2 if x.startswith(f"[ETM] Boot announce from WCB{me}")]
    edges = [x for _, x in p2 if x.startswith(f"[ETM] WCB{me} came ONLINE (boot)")]
    rx_beats = [t - t_sched for t, x in p2 if x.startswith(f"[ETM] Heartbeat from WCB{me}") and t > t_sched]
    notes.append(f"W1's debug on {lead:.2f} s after its ETM block; announces sent at {[round(s, 2) for s in sent]}, "
                 f"heard by W2 at {[round(s, 2) for s in heard]}; heartbeats sent {[round(s, 2) for s in beats[:1]]}, "
                 f"heard {[round(s, 2) for s in rx_beats[:1]]}")
    if lead < 1.35:                             # the debug flag was up before the first announce (1.5 s)
        gaps = [round(b - a, 2) for a, b in zip(sent, sent[1:])]
        if len(sent) != 3:
            problems.append(f"W1 sent {len(sent)} boot announce(s), not three")
        elif not 1.3 <= sent[0] <= 2.0 or any(not 1.0 <= g <= 1.5 for g in gaps):
            problems.append(f"W1's boot announces went out at {[round(s, 2) for s in sent]} s after its ETM block "
                            f"(expected ~1.5, then 1.2 s apart)")
    else:
        notes.append("W1's debug came on too late to see its own announces: W2's receive side judges them")
    if not 1 <= len(heard) <= 3:
        problems.append(f"W2 heard {len(heard)} boot announce(s) from W1 (one to three expected)")
    elif any(not any(abs(b - a - 1.2 * k) <= 0.3 for k in (1, 2)) for a, b in zip(heard, heard[1:])):
        problems.append(f"W2 heard W1's announces at {[round(s, 2) for s in heard]} s: not 1.2 s (or a lost one's 2.4 s) apart")
    if len(edges) != len(heard):
        problems.append(f"W2 printed {len(edges)} '(boot)' ONLINE edge(s) for {len(heard)} announce(s) heard")
    if lead < 0.85:                             # the flag was up before the earliest heartbeat (1 s)
        if not beats:
            problems.append(f"W1 sent no heartbeat within {max(6.0, boot_s + 1.5):.0f} s of its ETM block")
        elif not 0.85 <= beats[0] <= boot_s + 0.3:
            problems.append(f"W1's first heartbeat went {beats[0]:.2f} s after its ETM block, outside 1-{boot_s} s")
    elif rx_beats and rx_beats[0] < hb_s - 1.5:
        if not 0.85 <= rx_beats[0] <= boot_s + 0.35:
            problems.append(f"W2 heard W1's first heartbeat {rx_beats[0]:.2f} s after W1's ETM block, outside 1-{boot_s} s")
    else:
        notes.append("the first boot heartbeat was not seen (a lost broadcast): its window is not judged")
    ref = t_banner if t_banner is not None else t_task - 0.6
    row = next(((t, x) for t, x in dump if x.startswith(f"[WDP:N={me},")), None)
    age = re.search(r",AGE=(\d+),", row[1]) if row else None
    if age is None:
        problems.append("W2's DUMP has no row for W1 with an AGE")
    else:
        notes.append(f"W2's DUMP row for W1 {row[0] - ref:.1f} s after W1's banner: AGE={age.group(1)}")
        if row[0] - ref > 6.0:
            notes.append("the DUMP came more than 6 s after the banner: its AGE is not judged")
        elif int(age.group(1)) > 2:
            problems.append(f"W2's row for W1 was {age.group(1)} s old {row[0] - ref:.1f} s after W1's banner: no "
                            "boot advert refreshed it")
    bench.note("; ".join(notes))
    _peers_online(bench, w, strict=False)
    assert not problems, "; ".join(problems)


@test("wdp.advert_stagger_by_number", "A board's first periodic WDP advert after boot comes 60 s + (board number mod 16) x 500 ms after its '[WDP] discovery enabled': 61.0 s on W2, not the 60.5 s of WCB1's slot every board took while the number loaded late (re-scan #16; 1 W2 reboot, ~80 s)", needs=["wcb1", "wcb2"], links=[])
def advert_stagger_by_number(bench):
    """WCB-WP36 row 4 (re-scan #16, fixed: setup() loads WCB_Number first, WCB.ino:9361-9365, and wdpBegin runs at
    :9377). wdpBegin phases the periodic backstop by the board number and prints '[WDP] discovery enabled'
    (WCB_WDP.cpp:1969-1970); the 60 s reschedule adds no jitter (:383-386). The boot burst, a solicited advert and an
    on-change advert ride the other timer (:378-382, :364-371, :395-401) and print the same '[WDP] advert sent' line under
    ?DEBUG,MGMT (:344), so the advert timed is the first after 55 s that no '[WDP] solicited' line preceded by under
    1.5 s. Nothing polls during the window. Each line's host time is good to about 0.1 s (docs/HIL_TESTING.md §6); the
    two candidates are 0.5 s apart. Changes nothing (?DEBUG is RAM only)."""
    w = usb_wcb(bench)
    problems = []
    with Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its boot and advert lines cannot be read")
        toks2 = bench.config_tokens(2, refresh=True)
        n2 = int((token(toks2, "?WCB,") or "?WCB,2").split(",")[1])
        if "?WDP,OFF" in toks2 or "?ETM,ON" not in toks2:
            raise Skip("W2 has WDP or ETM off: it sends no adverts")
        if n2 % 16 == 1:
            raise Skip(f"W2 is WCB {n2}, in WCB1's slot: the fix and the defect look the same")
        expect = 60.0 + (n2 % 16) * 0.5
        w2 = WCB(c2.dev)
        try:
            m = w2.reboot()
            t_en = _t(list(c2.dev.lines[m:]), r"^\[WDP\] discovery enabled")
            if t_en is None:
                raise AssertionError("W2's boot printed no '[WDP] discovery enabled'")
            if not _has(w2.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled"):
                raise AssertionError("W2's ?DEBUG,MGMT,ON did not confirm")
            time.sleep(max(0.0, t_en + expect + 3.0 - time.monotonic()))
            pairs = list(c2.dev.lines[m:])
        finally:
            w2.run("?DEBUG,MGMT,OFF")
        solicited = [t for t, x in pairs if x.startswith("[WDP] solicited")]
        late = [round(t - t_en, 2) for t, x in pairs if x.startswith("[WDP] advert sent") and t - t_en >= 55.0
                and not any(0.0 <= t - s <= 1.5 for s in solicited)]
        if not late:
            problems.append(f"W2 sent no unsolicited advert between 55 and {expect + 3:.0f} s after '[WDP] discovery enabled'")
        else:
            bench.note(f"W2 (WCB{n2}): first periodic advert {late[0]} s after '[WDP] discovery enabled', expected {expect} s")
            if not expect - 0.25 <= late[0] <= expect + 0.3:
                problems.append(f"W2's first periodic advert came {late[0]} s after '[WDP] discovery enabled', not "
                                f"{expect} s (WCB1's slot is 60.5 s)")
        _peers_online(bench, w, strict=False)
    assert not problems, "; ".join(problems)


# ============================================================ non-default settings across a reboot (WCB-WP47)
def _chars_back(w, problems):
    """Put the delimiter and both prefix characters back to ^ ? ; and check that they are. s18's _restore_prefixes and
    _prefix_chars read the live ones with WCB_WEBTOOL_CONFIG_PULL, which is recognised whatever they are, and send only
    what differs. WCB.run and config_guard need the defaults back before anything else is sent, so when they cannot be
    confirmed this raises at once, with the test's problems so far and the lines to type."""
    try:
        _restore_prefixes(w)
        chars = _prefix_chars(w)
    except AssertionError as e:
        chars = f"unreadable ({(str(e).splitlines() or [repr(e)])[0]})"
    if chars != ("^", "?", ";"):
        problems.append(f"RESTORE NOT CONFIRMED: W1's delimiter, function identifier and command character are {chars}, "
                        "not ^ ? ; - on W1's USB console type <its function identifier>FUNCCHAR,? and ?CMDCHAR,;")
        raise AssertionError("; ".join(problems))


@test("chars.cmdchar_reboot", "?CMDCHAR,: survives a W1 reboot: the boot banner and ?config show ':' and ':S0' runs after the boot; ?CMDCHAR,; puts it back before any chain is compared (1 reboot)", needs=["wcb1"], links=[])
def cmdchar_reboot(bench):
    """WCB-WP47 row 1. saveLocalFunctionIdentifierAndCommandCharacter stores both prefixes (WCB_Storage.cpp:595-600) and
    setup() loads them (loadLocalFunctionIdentifierAndCommandCharacter, WCB_Storage.cpp:580-593, from WCB.ino:9695) and
    prints them in its General Settings block (:9707-9710). While ':' is the command character, WCB.run's ';S0' sentinel
    is a broadcast, so the window uses dev.send; WCB.reboot sends only '?' lines, which still work. The character goes
    back in the finally, before config_guard reads the chain (CMDCHAR is in NO_AUTO_RESTORE_WHILE, suites/common.py)."""
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?CMDCHAR,;")
    t = marker()
    problems = []
    with config_guard(bench, 1):
        try:
            _sent(w, "?CMDCHAR,:", r"^Command character updated to ':'")
            bm = w.reboot()
            if "Command Character: :" not in [x.rstrip() for x in w.dev.since(bm)]:
                problems.append("the boot banner does not show ':' as the command character")
            try:
                _sent(w, f":S0{t}", rf"^{t}$")
            except AssertionError:
                problems.append("':S0' did not run after the boot")
            try:
                _sent(w, "?config", r"Command Character:\s+:", timeout=5)
            except AssertionError:
                problems.append("?config does not show ':' after the boot")
        finally:
            _chars_back(w, problems)
        _peers_online(bench, w, strict=False)
    assert not problems, "; ".join(problems)


@test("chars.funcchar_reboot", "?FUNCCHAR,! survives a W1 reboot sent as '!reboot': the boot banner shows '!', and !VERSION and !CONFIG answer after the boot; !FUNCCHAR,? puts it back before any chain is compared (1 reboot)", needs=["wcb1"], links=[])
def funcchar_reboot(bench):
    """WCB-WP47 row 1. The same storage and load as chars.cmdchar_reboot (WCB_Storage.cpp:580-600, WCB.ino:9695, :9709).
    While '!' is the function identifier a '?' line is broadcast text that reaches W2 and NaviCore, so nothing here sends
    one - not WCB.reboot or WCB.run, not even wait_boot's ?VERSION: the reboot is '!reboot' and the boot is waited out by
    hand. The identifier goes back in the finally (_chars_back)."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1):
        try:
            _sent(w, "?FUNCCHAR,!", r"^Local function identifier updated to '!'")
            m = w.dev.mark()
            w.dev.send("!reboot")
            w.dev.expect(r"^Reboot queued", timeout=3, since=m)
            w.dev.expect(r"^Rebooting now", timeout=w.REBOOT_DEFER_S, since=m)
            w.dev.expect(rf"^{FORWARDING}", timeout=30, since=m)
            time.sleep(1.5)                          # the USB reader starts 500 ms after its task (WCB.ino:8697)
            if "Local Function Identifier: !" not in [x.rstrip() for x in w.dev.since(m)]:
                problems.append("the boot banner does not show '!' as the function identifier")
            try:
                _sent(w, "!VERSION", r"^End of Version")
            except AssertionError:
                problems.append("!VERSION did not answer after the boot")
            try:
                _sent(w, "!CONFIG", r"Local Function Identifier: !", timeout=5)
            except AssertionError:
                problems.append("!CONFIG does not show '!' after the boot")
        finally:
            _chars_back(w, problems)
        _peers_online(bench, w, strict=False)
    assert not problems, "; ".join(problems)


DEBUG_ONLY = ("Sent to USB: ", "[ETM] Sent seq ", "[ETM] Heartbeat sent", "[ETM] Next heartbeat in",
              "[ETM] Boot announce sent", "Processing ETM input from")   # printed only under ?DEBUG / ?DEBUG,ETM


@test("persist.flags_and_etm_reboot", "Non-default ?WDP,AUTOJOIN,OFF, ?BCAST,OUT,S0,ON and ETM MISS / BOOT / COUNT / DELAY survive a W1 reboot (the chain, ?config, WDPCFG, ?WDP,AUTOJOIN, a typed broadcast echoed to USB, W1's first heartbeat inside the new boot window) while ?DEBUG and ?DEBUG,ETM, RAM only, come back off; all put back afterwards (1 reboot)", needs=["wcb1", "wcb2"], links=[])
def flags_and_etm_reboot(bench):
    """WCB-WP47 row 1: the settings with no reboot test of their own (etm.settings_persist_reboot has ETM HB and TIMEOUT,
    persist.bcast_flags_reboot the S1-S5 flags). Auto-join: saveWdpSettings / loadWdpSettings (WCB_WDP.cpp:1945-1957,
    loaded by wdpBegin, WCB.ino:9377); there is no boot line for it. S0 echo: the S0 key of the broadcast namespace
    (WCB_Storage.cpp:383-406), echoed by processBroadcastCommand even for a broadcast typed on S0 (WCB.ino:8349-8352).
    ETM: saveETMSettings / loadETMSettings (WCB_Storage.cpp:2821-2861, loaded at WCB.ino:9696). The debug flags are plain
    globals that ?DEBUG sets (WCB.ino:6084-6108) and nothing saves. The boot heartbeat is drawn from 1 s to the new
    ETM,BOOT (:1339-1345) and judged on W2's receive side, since W1's own debug has to be seen to come back off; a first
    heartbeat W2 missed is noted, not failed. Rebooting with ETM or CHKSM off stays opt-in (CLAUDE.md rule 4): not here."""
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    t_s0, t_echo = marker("s"), marker("e")
    problems, notes = [], []
    with config_guard(bench, 1) as before, Console(bench, 2) as c2:
        toks = before[1]
        if c2.remote:
            raise Skip("W2 has no USB console here: W1's heartbeats cannot be timed on it")
        if "?WDP,OFF" in toks or "?WDP,AUTOJOIN,OFF" in toks:
            raise Skip("W1 already has WDP or auto-join off")
        if "?BCAST,OUT,S0,OFF" not in toks or "?ETM,ON" not in toks:
            raise Skip("W1's S0 echo is not off, or its ETM is off")
        orig = {}
        for k in ("MISS", "BOOT", "COUNT", "DELAY", "HB"):
            tk = token(toks, f"?ETM,{k},")
            if tk is None:
                raise Skip(f"W1's chain lacks ?ETM,{k}")
            orig[k] = int(tk.split(",")[2])
        new = {"MISS": orig["MISS"] + 1 if orig["MISS"] < 100 else 99, "BOOT": 6 if orig["BOOT"] != 6 else 5,
               "COUNT": 30 if orig["COUNT"] != 30 else 25, "DELAY": 150 if orig["DELAY"] != 150 else 120}
        said = {"MISS": "ETM missed heartbeats set to {}", "BOOT": "ETM boot window set to {} sec",
                "COUNT": "ETM char message count set to {}", "DELAY": "ETM char delay set to {} ms"}
        try:
            if not _has(w.run("?WDP,AUTOJOIN,OFF"), "[WDP] auto-join disabled"):
                problems.append("?WDP,AUTOJOIN,OFF did not confirm")
            out = w.run("?BCAST,OUT,S0,ON")
            if _has(out, "NVS could not store"):
                raise Skip("W1's NVS refused the S0 flag, which the reboot is to reload (see ?NVS)")
            if not _has(out, "Broadcast OUTPUT on S0 (USB): Enabled"):
                problems.append(f"?BCAST,OUT,S0,ON printed {out}")
            for k, v in new.items():
                if not _has(w.run(f"?ETM,{k},{v}"), said[k].format(v)):
                    problems.append(f"?ETM,{k},{v} did not confirm")
            if not _has(_run(c2, "?DEBUG,ETM,ON"), "ETM debugging enabled"):
                problems.append("W2's ?DEBUG,ETM,ON did not confirm")
            for cmd, want in (("?DEBUG,ON", "Debugging enabled"), ("?DEBUG,ETM,ON", "ETM debugging enabled")):
                if not _has(w.run(cmd), want):
                    problems.append(f"{cmd} did not confirm")
            cm = c2.mark()
            bm = w.reboot()
            t_sched = _t(list(w.dev.lines[bm:]), r"^\[ETM\] Heartbeat scheduled \(boot window\)")
            # RAM-only debug is off again: neither ;S0 nor a unicast prints a debug line
            w.run(f";S0{t_s0}")
            w.send(";W2,?PEERSLIVE")
            time.sleep(1.5)
            leaked = [x for x in _after_boot(w.dev, bm) if x.startswith(DEBUG_ONLY)]
            if leaked:
                problems.append(f"after the reboot W1 printed debug lines: {leaked[:3]}")
            if not _has(w.run("?WDP,AUTOJOIN"), "[WDP] auto-join is OFF"):
                problems.append("?WDP,AUTOJOIN does not report OFF after the reboot")
            if not any(x.startswith("[WDPCFG:EN=1,AUTOJOIN=0,") for x in w.run("?WDP,DUMP", timeout=8)):
                problems.append("WDPCFG does not show AUTOJOIN=0 after the reboot")
            after = snapshot(bench, 1)
            problems += [f"after the reboot the chain lacks {x}" for x in
                         ["?WDP,AUTOJOIN,OFF", "?BCAST,OUT,S0,ON"] + [f"?ETM,{k},{v}" for k, v in new.items()]
                         if x not in after]
            m = w.dev.mark()
            w.dev.send(t_echo)                       # a plain broadcast typed on S0: echoed back to S0 while the flag is on
            try:
                w.dev.expect(rf"^{t_echo}$", timeout=3, since=m)
            except AssertionError:
                problems.append("after the reboot a broadcast typed on USB was not echoed back to it")
            cfg = [x.rstrip() for x in w.run("?config")]
            problems += [f"?config after the reboot lacks {x!r}" for x in (
                f"Boot heartbeat:       1-{new['BOOT']} sec",
                f"Offline after:        {new['MISS']} missed heartbeats ({(orig['HB'] + 1) * new['MISS']} sec max)",
                f"Char message count:   {new['COUNT']}  delay: {new['DELAY']} ms") if x not in cfg]
            if t_sched is None:
                problems.append("W1's boot printed no '[ETM] Heartbeat scheduled (boot window)'")
            else:
                time.sleep(max(0.0, t_sched + new["BOOT"] + 0.6 - time.monotonic()))
                rx = [t - t_sched for t, x in list(c2.dev.lines[cm:])
                      if x.startswith(f"[ETM] Heartbeat from WCB{me}") and t > t_sched]
                if not rx:
                    notes.append(f"W2 heard no heartbeat from W1 within 1-{new['BOOT']} s of its ETM block (a lost "
                                 "broadcast): the new boot window is not judged")
                elif not 0.85 <= rx[0] <= new["BOOT"] + 0.35:
                    problems.append(f"W2 heard W1's first heartbeat {rx[0]:.2f} s after W1's ETM block, outside the "
                                    f"new 1-{new['BOOT']} s window")
                else:
                    notes.append(f"W1's first heartbeat reached W2 {rx[0]:.2f} s after its ETM block (window 1-{new['BOOT']} s)")
        finally:
            _run(c2, "?DEBUG,ETM,OFF")
            w.run("?DEBUG,OFF")
            w.run("?DEBUG,ETM,OFF")
            w.run("?WDP,AUTOJOIN,ON")
            w.run("?BCAST,OUT,S0,OFF")
            for k in new:
                w.run(f"?ETM,{k},{orig[k]}")
        if notes:
            bench.note("; ".join(notes))
        _peers_online(bench, w, strict=False)
    assert not problems, "; ".join(problems)


@test("peers.controller_off_reboot_readopted", "?CONTROLLER,OFF survives a W1 reboot - the boot registers no special peer and ?CONTROLLER says DISABLED - until NaviCore's first advert after the boot adopts it again ('heard controller ... auto-enabling', ENABLED, ?CONTROLLER,ON,20 back in the chain), because the WDP neighbour table lives in RAM (auto-join off; 1 reboot)", needs=["wcb1"], links=[])
def controller_off_reboot_readopted(bench):
    """WCB-WP47 row 2, the OFF half (peers.controller_other_id_persist in s18 is the ON,<id> half). ?CONTROLLER,OFF saves
    the flag (saveSpecialPeerPreferences, WCB_Storage.cpp:482-489), and setup() registers the special peer only when it
    is set (WCB.ino:9646-9662). wdpBegin empties the neighbour table at boot (WCB_WDP.cpp:1959-1962), so NaviCore's next
    advert is a first learn, and the first learn of a controller device with no controller enabled enables it
    (WCB_WDP.cpp:637-650; docs/WDP_DESIGN.md §9): an operator's OFF lasts only until NaviCore is heard after a boot.
    Auto-join is off throughout, as in s18's controller tests, so NaviCore is never learned as a persisted peer.
    ?WDP,POLL asks for its advert, up to three times: the solicit and the answer are single broadcasts."""
    w = usb_wcb(bench)
    adopted = re.compile(r'^\[WDP\] heard controller ".*" \(WCB20\) — auto-enabling controller peer')
    problems = []
    with config_guard(bench, 1) as before:
        if "?CONTROLLER,ON,20" not in before[1]:
            raise Skip("controller 20 is not enabled on W1")
        if "?WDP,OFF" in before[1] or "?WDP,AUTOJOIN,OFF" in before[1]:
            raise Skip("W1 has WDP or auto-join off")
        if not any(x.startswith("[WDP:N=20,") for x in w.run("?WDP,DUMP", timeout=8)):
            raise Skip("NaviCore is not in W1's WDP table")
        try:
            if not _has(w.run("?WDP,AUTOJOIN,OFF"), "[WDP] auto-join disabled"):
                raise AssertionError("?WDP,AUTOJOIN,OFF did not confirm")
            if not _has(w.run("?CONTROLLER,OFF"), "Controller peer (ID 20) DISABLED."):
                problems.append("?CONTROLLER,OFF did not confirm")
            bm = w.reboot()
            lines = [x.rstrip() for x in w.dev.since(bm)]
            if any(x.startswith("Added ESP-NOW special peer") for x in lines):
                problems.append("the boot registered a special peer although the controller was saved OFF")
            if any(adopted.search(x) for x in lines):
                bench.note("NaviCore's advert adopted the controller again before ?CONTROLLER could be read")
            elif not _has(w.run("?CONTROLLER"), "Controller peer (ID 20) is currently DISABLED."):
                problems.append("right after the boot ?CONTROLLER does not say DISABLED")
            got = any(adopted.search(x) for x in w.dev.since(bm))
            for attempt in range(3):
                if got:
                    break
                w.run("?WDP,POLL")
                try:
                    w.dev.expect(adopted.pattern, timeout=3, since=bm)
                    got = True
                except AssertionError:
                    pass
            if not got:
                problems.append("NaviCore was not heard, or not adopted as the controller, after three polls")
            else:
                if not _has(w.run("?CONTROLLER"), "Controller peer (ID 20) is currently ENABLED."):
                    problems.append("after the adoption ?CONTROLLER does not say ENABLED")
                if "?CONTROLLER,ON,20" not in snapshot(bench, 1):
                    problems.append("after the adoption the chain lacks ?CONTROLLER,ON,20")
            if _has(w.dev.since(bm), "[WDP] auto-joined WCB20"):
                problems.append("NaviCore was joined as a learned peer")
        finally:
            if not _has(w.run("?CONTROLLER"), "Controller peer (ID 20) is currently ENABLED."):
                w.run("?CONTROLLER,ON,20")
            w.run("?WDP,AUTOJOIN,ON")
        _peers_online(bench, w, strict=False)
    assert not problems, "; ".join(problems)


@test("boot.devices_onerr_volume_w2", "The MP3 and DFPlayer ONERR keys and current volumes survive a W2 reboot: the [..] Loaded lines carry the changed current volumes (30 and 25 against defaults of 20), both LISTs show 'On Error : HILE', and after the boot an injected MP3 'E' and a DFPlayer error frame each recall HILE (W2 reboot)", needs=["wcb1"], links=["W2S3", "W2S4", "W2S5"])
def devices_onerr_volume_w2(bench):
    """WCB-WP47 row 3. saveMP3Settings / saveDFPSettings store the ONERR key and the current volume ('vol') beside the
    default ('defvol') (WCB_MP3.cpp:430-440, WCB_DFP.cpp:390-399); a volume verb saves the shadow through the codec's
    onVolumeChanged hook (WCB_MP3.cpp:47-51, WCB_DFP.cpp:43-47); the loaders print both volumes at boot
    (WCB_MP3.cpp:457-461, WCB_DFP.cpp:417-421). boot.devices_restore_w2 covers the ports and the bytes, with default
    volumes and no ONERR. HILE writes ;S4<marker> on W2 (a recalled ONERR runs with local origin, s15). Cleared as
    boot.devices_restore_w2 clears, plus the HILE sequence."""
    s3, s4, s5 = link(bench, 2, "S3"), link(bench, 2, "S4"), link(bench, 2, "S5")
    for p in ("S3", "S4", "S5"):
        _require_free(bench, 2, p)
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has MP3/DFP config")
    _no_recall_keys(bench)
    w = usb_wcb(bench)
    err = f"hilerr{nonce().lower()}"
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its boot lines cannot be read")
        saved = touched = False
        try:
            out = _run(c2, f"?SEQ,SAVE,HILE,;S4{err}", 1.0)
            if _has(out, "Failed to store sequence"):
                raise Skip("W2's NVS refused the HILE sequence (see ?NVS)")
            saved = _has(out, "Stored: Key='HILE'")
            if not saved:
                raise AssertionError(f"setup: ?SEQ,SAVE,HILE printed {out}")
            touched = True
            for cmd, want in (("?MP3,S3:9600:V20", "[MP3] Configured: S3 at 9600 baud  default volume=20"),
                              ("?DFP,S5", "[DFP] Configured: S5 at 9600 baud  default volume=20"),
                              ("?MP3,ONERR,HILE", "[MP3] Error callback → ;CHILE"),
                              ("?DFP,ONERR,HILE", "[DFP] Error callback → ;CHILE"),
                              (";A,VOL,30", "[MP3] Volume → 30"),
                              (";D,VOL,25", "[DFP] Volume → 25")):
                if not _has(_run(c2, cmd, 1.0), want):
                    problems.append(f"{cmd} did not print {want!r}")
            time.sleep(2)
            bm = c2.mark()
            _reboot_w2(w)
            boot = [x.rstrip() for x in c2.lines(bm) if "password" not in x.lower()]
            problems += [f"the boot lacks {x!r}" for x in (
                "[MP3] Loaded: S3 at 9600 baud  default vol=20  current vol=30",
                "[DFP] Loaded: S5 at 9600 baud  default vol=20  current vol=25") if x not in boot]
            for kind, vol in (("MP3", 30), ("DFP", 25)):
                lst = _run(c2, f"?{kind},LIST", 1.0)
                if not _has(lst, "  On Error      : HILE"):
                    problems.append(f"after the boot ?{kind},LIST does not show 'On Error : HILE'")
                if not _has(lst, f"  Current Volume: {vol}"):
                    problems.append(f"after the boot ?{kind},LIST does not show 'Current Volume: {vol}'")
            time.sleep(0.15)                         # 100 ms+ between a frame out and an injection (s15)
            lines, got = _inject(c2, s3, s4, b"E")
            if not _has(lines, "[MP3] Error: track not found or device error") or err.encode() + b"\r" not in got:
                problems.append(f"after the boot an MP3 'E' did not recall HILE: {lines} / S4 {got!r}")
            time.sleep(0.15)
            lines, got = _inject(c2, s5, s4, _dfp(0x40, 6))
            if not _has(lines, "[DFP] Error 0x06") or err.encode() + b"\r" not in got:
                problems.append(f"after the boot a DFPlayer error frame did not recall HILE: {lines} / S4 {got!r}")
        finally:
            if touched:
                _clear_all_w2(c2)
                _relabel(c2, before[2], "S3", "S5")
            if saved:
                _run(c2, "?SEQ,CLEAR,HILE")
            if touched:                              # W1 learned both hosts from W2's adverts, and persisted them
                _unlearn(bench, "MP3", learner=1)
                _unlearn(bench, "DFP", learner=1)
    assert not problems, "; ".join(problems)
