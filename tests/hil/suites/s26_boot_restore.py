"""Boot-time restore per subsystem (docs/HIL_TEST_AUDIT.md WP2): a setting saved to NVS is what the board runs after a
reboot - checked on the boot banner, in the config chain, and in behaviour (bytes on a wire, a refused command).

Labels, ETM settings, serial and PWM mappings, variables and the Kyber modes already have reboot tests (s06, s13,
s14, s16, s18, s22). This suite covers the rest: the HCR / MP3 / DFPlayer host configs and W1's learned routes, a
local WLED, the alias, explicit ?BCAST flags, the delimiter, WDP off, WCBQ, and a remote Maestro proxy.

Every test restores in a finally and runs under config_guard; W1 reboots seven times in all (~12 s each) and W2 once.
Nothing here moves a servo.
"""
import re
import time

from hil.runner import Skip, test
from suites.common import Console, Watch, config_guard, link, marker, require_tokens, snapshot, token, usb_wcb
from suites.s15_hcr_mp3_dfp import (_clear_all_w2, _device_tokens, _dfp, _in_order, _lf, _relabel, _require_free,
                                     _run, _steps, _unlearn)
from suites.s24_wled_config import ON as WLED_ON, _ids_free, _wdp_off
from suites.s24_wled_config import _relabel as _relabel_w      # the WCB (not Console) flavour


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
