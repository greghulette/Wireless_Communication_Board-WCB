"""HCR, MP3 Trigger and DFPlayer — config, ;H/;A/;D bytes, routing, WDP learning, reply parsing.

Built from the verified hcr_mp3_dfp specs. No real devices are on the bench: the probe checks the bytes the WCB
writes and plays the device's replies (hcr.cpp:263-360, WcbMp3.cpp:95-136, WcbDfPlayer.cpp:170-206).

Rules from the specs:
- CLEAR is not an undo. It forces 9600, broadcast ON/ON and deletes the label (WCB_HCR.cpp:459-473,
  WCB_MP3.cpp:165-179, WCB_DFP.cpp:127-141), so only ports whose baseline is exactly that are used, and the
  baseline label is re-issued afterwards.
- MP3 or DFP hosted anywhere, or HCR hosted on W1, make the other board PERSIST a learned route (first-host-wins,
  never evicted, WCB_WDP.cpp:704-735). _unlearn() is the only order that undoes it.
- W1 already stores ?HCR,REMOTE,W2, so HCR hosted on W2 changes nothing on W1.
- ?HCR,POLL,OFF goes before ?HCR,PORT: the next loop pass polls with POLL (WCB_HCR.cpp hcrPoll).
- The poll is ONE container frame, POLL below: soft-serial RX is masked while the WCB transmits (rule 13), so
  back-to-back queries lost every reply but the last. ?HCR,STATUS and ?DEBUG,HCR never transmit.
- Recalled ONFIN/ONERR sequences run with local origin, so they hold ;S3 markers only.
"""
import re
import time
from contextlib import contextmanager

from hil.navicore import DBG_MAESTRO, NaviCore
from hil.runner import Skip, test
from suites.common import (NOISE, Console, Watch, config_guard, link, marker, nonce, probe_in_mesh, require_tokens,
                           snapshot, token, usb_wcb)
from suites.s14_pwm import _no_pwm, _pwm_reboot
from suites.s22_maestro_kyber import _wdp_off

LEARNED = {"HCR": ("HCR", "H"), "MP3": ("MP3", "A"), "DFP": ("DFPlayer", "D")}


# ------------------------------------------------------------------ helpers
def _has(lines, text):
    return any(text in x for x in lines)


def _in_order(lines, wanted):
    """The wanted lines appear in `lines` in this order (exact after rstrip); returns the first one missing."""
    i = 0
    for x in (y.rstrip() for y in lines):
        if i < len(wanted) and x == wanted[i]:
            i += 1
    return None if i == len(wanted) else wanted[i]


def _device_tokens(bench, wcb, *kinds):
    kinds = kinds or ("HCR", "MP3", "DFP")
    return [t for t in bench.config_tokens(wcb, refresh=True) if t.upper().startswith(tuple(f"?{k}," for k in kinds))]


def _require_free(bench, wcb, port):
    """Skip unless W<wcb> <port> is what CLEAR restores it to: 9600, broadcast ON/ON, no device or mapping."""
    require_tokens(bench, wcb, f"?BAUD,{port},9600", f"?BCAST,OUT,{port},ON", f"?BCAST,IN,{port},ON")
    # (?:\b|W\d+) also catches W<n>S<p> forms such as ?WLED,1:W2S2:115200, where \b alone never matches; a token
    # naming another board's port of the same number skips the test too, which errs on the safe side.
    busy = [t for t in bench.config_tokens(wcb)
            if re.search(rf"(?:\b|W\d+){port}\b", t) and not t.upper().startswith(("?BAUD,", "?BCAST,", "?LABEL,", "?SEQ,"))]
    if busy:
        raise Skip(f"W{wcb} {port} is in use: {busy}")


def _labels(tokens):
    return {t.split(",")[1].upper(): t.split(",", 2)[2] for t in tokens if t.upper().startswith("?LABEL,") and t.count(",") >= 2}


def _relabel(console, before, *ports):
    """CLEAR deletes a port's label; put back whatever the baseline had."""
    labels = _labels(before)
    for p in ports:
        if p in labels:
            m = console.send(f"?LABEL,{p},{labels[p]}")
            console.expect(rf"Serial{p[1]} label set to: '", since=m)


def _run(console, cmd, wait=0.8):
    """Console lines printed within `wait` of one command (works for USB and RTERM consoles)."""
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _steps(l, send, steps, settle=0.35):
    """Send each (command, expected bytes); the wire must carry exactly those bytes before the next command.
    Returns the mismatches, so the caller's restore still runs before anything is asserted."""
    bad = []
    for cmd, want in steps:
        m = l.mark()
        send(cmd)
        if want:
            try:
                l.expect(want, timeout=2.0, since=m)
            except AssertionError:
                pass
        time.sleep(settle)
        got = l.received(m)
        if got != want:
            bad.append(f"{cmd}: expected {want!r}, got {got!r}")
    return bad


POLL = b"<QM,QD,QVV,QVA,QVB>\n"   # WCB_HCR.cpp HCR_POLL_FRAME
POLL_HEX = POLL.hex().upper()


def _frame_times(l, since, frame):
    """(all bytes since the mark, probe millis() of the start of each `frame` in them)."""
    stream, starts = b"", []
    for ms, chunk in l.bursts(since):
        starts.append((len(stream), ms))
        stream += chunk
    times, i = [], stream.find(frame)
    while i >= 0:
        times.append([ms for s, ms in starts if s <= i][-1])
        i = stream.find(frame, i + len(frame))
    return stream, times


def _unlearn(bench, kind, learner):
    """Undo a route W<learner> auto-learned and PERSISTED. Only this order works: the host has already dropped
    the device; let its changed advert be decoded; clear the learner; poll again and check the route does not
    come back (auto-add runs only on advert decode, WCB_WDP.cpp:704-735)."""
    w = usb_wcb(bench)
    time.sleep(3)
    w.run("?WDP,POLL")
    time.sleep(2)
    with Console(bench, learner) as c:
        m = c.send(f"?{kind},REMOTE,OFF")
        c.expect(rf"\[{kind}\] Remote routing cleared", since=m)
        m = c.mark()
        w.run("?WDP,POLL")
        time.sleep(3)
        again = [x for x in c.lines(m) if "host learned" in x]
    assert not again, f"W{learner} re-learned the route after it was cleared: {again}"


@contextmanager
def _hcr_w2(bench, port="S4", poll="OFF", debug=False):
    """FX-H4: HCR hosted on W2 <port> with the poll pinned; on exit RS-H4 (CLEAR, relabel). W1 already routes
    ;H to W2, so nothing propagates. Yields W2's console, with the configuration output in .config_lines.
    debug=True wraps only the config step in ?DEBUG,ON/OFF (it floods the console otherwise)."""
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    _require_free(bench, 2, port)
    if _device_tokens(bench, 2):
        raise Skip(f"W2 already has HCR/MP3/DFP config: {_device_tokens(bench, 2)}")
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        m = c2.send(f"?HCR,POLL,{poll}")
        c2.expect(r"\[HCR\] Poll interval = ", since=m)
        if debug:
            c2.expect(r"Debugging enabled", since=c2.send("?DEBUG,ON"))
        m = c2.send(f"?HCR,PORT,{port}:9600")
        try:
            c2.expect(rf"\[HCR\] Configured on {port} at 9600 baud", since=m)
            c2.config_lines = c2.lines(m)
            if debug:
                c2.expect(r"Debugging disabled", since=c2.send("?DEBUG,OFF"))
            yield c2
        finally:
            if debug:
                c2.send("?DEBUG,OFF")
            m = c2.send("?HCR,CLEAR")
            c2.expect(rf"  ✓ Released {port} \(old HCR port\)", since=m)
            _relabel(c2, before[2], port)


@contextmanager
def _w2_audio(bench, kind, cfg, port):
    """MP3 or DFP hosted on W2 <port>, after W1 has learned the route; on exit CLEAR, relabel, and undo W1's
    persisted route. Yields W2's console."""
    name, verb = LEARNED[kind]
    _require_free(bench, 2, port)
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has MP3/DFP config")
    w = usb_wcb(bench)
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        m1 = w.dev.mark()
        m = c2.send(f"?{kind},{cfg}")
        try:
            c2.expect(rf"\[{kind}\] Configured: {port} at 9600 baud", since=m)
            c2.config_lines = c2.lines(m)
            w.dev.expect(rf"\[WDP\] {name} host learned — routing ;{verb} to WCB2", timeout=5, since=m1)
            yield c2
        finally:
            m = c2.send(f"?{kind},CLEAR")
            c2.expect(rf"\[{kind}\] Local configuration cleared", since=m)
            time.sleep(0.5)
            _relabel(c2, before[2], port)
            _unlearn(bench, kind, learner=1)


# ============================================================ routing with no device
@test("hcr.route_to_unconfigured_host", "W1's stored route forwards ;H to W2, which has no HCR; W1's CLEAR keeps the route", needs=["wcb1"], links=[])
def route_to_unconfigured_host(bench):
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    if _device_tokens(bench, 2):
        raise Skip("W2 has HCR/MP3/DFP config")
    w = usb_wcb(bench)
    with config_guard(bench, 1, 2), Console(bench, 2) as c2:
        try:
            w.run("?DEBUG,ETM,ON")
            m, cm = w.dev.mark(), c2.mark()
            w.send(";H,OVERLOAD")
            w.dev.expect(r"\[ROUTE\] ;H,OVERLOAD -> host WCB2", timeout=3, since=m)
            c2.expect(r"\[HCR\] Not configured — use \?HCR,PORT,Sx:baud first", timeout=4, since=cm)
            assert _has(w.run("?HCR,LIST"), "  Routes ;H to WCB2 (remote host)")
            assert _has(w.run("?HCR,STATUS"), "[HCR:cfg=0]")
            out = w.run("?HCR,CLEAR")
            assert _has(out, "[HCR] Local configuration cleared") and _has(out, "still routing ;H to WCB2"), out
            assert not _has(out, "✓"), f"CLEAR released a port although none was configured: {out}"
        finally:
            w.run("?DEBUG,ETM,OFF")


@test("mp3dfp.not_configured", ";A/;D with no host anywhere run locally, report not configured, and write nothing", needs=["wcb1"], links=[])
def not_configured(bench):
    if _device_tokens(bench, 1, "MP3", "DFP") or _device_tokens(bench, 2, "MP3", "DFP"):
        raise Skip("an MP3/DFP host is configured")
    w = usb_wcb(bench)
    wires = [l for l in (bench.links.get(1, p) for p in ("S2", "S3", "S4", "S5")) if l]
    watch = Watch(*wires)
    assert _has(w.run(";A,PLAY,1"), "[MP3] Not configured — use ?MP3,S<port>:<baud>:V<vol>")
    assert _has(w.run(";D,PLAY,1"), "[DFP] Not configured — use ?DFP,S<port>[:9600][:V<vol>]")
    mp3 = [x.rstrip() for x in w.run("?MP3,LIST")]
    dfp = [x.rstrip() for x in w.run("?DFP,LIST")]
    miss = _in_order(mp3, ["--- MP3 Trigger Configuration ---", "  Not configured.",
                           "  Use: ?MP3,S<port>:<baud>:V<vol>  (e.g. ?MP3,S2:9600:V25)"])
    assert miss is None, f"?MP3,LIST lacks {miss!r}: {mp3}"
    miss = _in_order(dfp, ["--- DFPlayer Mini Configuration ---", "  Not configured.",
                           "  Use: ?DFP,S<port>[:9600][:V<vol>]  (e.g. ?DFP,S2:9600:V20)"])
    assert miss is None, f"?DFP,LIST lacks {miss!r}: {dfp}"
    watch.silent(*wires)


# ============================================================ HCR hosted on W2 S4
HCR_RELEASE_S4 = ["[HCR] Local configuration cleared", "  ✓ Re-enabled broadcast output on S4",
                  "  ✓ Re-enabled broadcast input on S4", "Baud rate for Serial4 updated to 9600",
                  "Serial4 label set to: ''", "  ✓ Released S4 (old HCR port)"]


@test("hcr.config_softport", "?HCR,PORT on W2 S4 (soft serial): console lines, reserved port, immediate poll, 3 s retries until the volumes seed, then 10 s, exact restore (~45 s)", needs=["wcb1"])
def config_softport(bench):
    s4 = link(bench, 2, "S4")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip("W2 has HCR/MP3/DFP config")
    w = usb_wcb(bench)
    qd = POLL
    clear = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        configured = False
        try:
            m = c2.send("?HCR,POLL,10")
            c2.expect(r"\[HCR\] Poll interval = 10s", since=m)
            # Answer every poll with all three volumes: seeding needs two agreeing replies per channel, so the
            # WCB retries every 3 s until then (at most 10 times) and only then settles on pollSec.
            s4.rule(1, POLL_HEX, b"<QVV,50>\n<QVA,50>\n<QVB,50>\n", delay_ms=50)
            pm, wm = s4.mark(), w.dev.mark()
            cm = c2.send("?HCR,PORT,S4:9600")
            configured = True
            t0 = time.monotonic()
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=cm)
            s4.expect(qd, timeout=1.5, since=pm)
            cfg = c2.lines(cm)
            miss = _in_order(cfg, ["  ⚠️  Disabled broadcast output on S4 (HCR port)", "  ⚠️  Disabled broadcast input on S4 (HCR port)",
                                   "Serial4 label set to: 'HCR'", "[HCR] Active on S4 at 9600 baud, poll=10s",
                                   "[HCR] Configured on S4 at 9600 baud"])
            assert miss is None, f"config output lacks {miss!r}: {cfg}"
            assert not _has(cfg, "Baud rate for Serial4"), "the baud was re-applied although it was already 9600"
            time.sleep(3)
            assert not _has(w.dev.since(wm), "host learned"), "W1 learned an HCR host although it already stores one"
            lst = _run(c2, "?HCR,LIST")
            miss = _in_order(lst, ["---- HCR Configuration ----", "  Port:  S4", "  Baud:  9600", "  Poll:  10s", "  Link:  active"])
            assert miss is None, f"?HCR,LIST lacks {miss!r}: {lst}"
            tokens = snapshot(bench, 2)
            for t in ("?BCAST,OUT,S4,OFF", "?BCAST,IN,S4,OFF", "?LABEL,S4,HCR", "?HCR,PORT,S4:9600"):
                assert t in tokens, f"W2 config lacks {t}"
            i = tokens.index("?HCR,PORT,S4:9600")
            assert tokens[i + 1:i + 2] == ["?HCR,POLL,10"], f"?HCR,POLL does not follow ?HCR,PORT: {tokens[i:i + 2]}"
            time.sleep(max(0.0, 25 - (time.monotonic() - t0)))
            stream, times = _frame_times(s4, pm, qd)
            gaps = [b - a for a, b in zip(times, times[1:])]
            bench.note(f"poll gaps on the probe clock: {gaps}")
            assert stream == qd * len(times), f"bytes other than the poll on W2 S4: {stream!r}"
            fast = [g for g in gaps if 2950 <= g <= 3400]
            slow = [g for g in gaps if 9950 <= g <= 10400]
            # 3 s retries first (one per garbled/unseeded round), then the 10 s cadence and never back.
            assert len(fast) + len(slow) == len(gaps) and gaps == fast + slow, f"poll gaps {gaps}"
            assert fast and slow, f"poll gaps {gaps}: expected 3 s retries until seeded, then 10 s"
            assert not s4.errors(pm), f"RXERR on W2 S4: {s4.errors(pm)}"
        finally:
            s4.probe.rule_del(1)
            if configured:
                m = c2.send("?HCR,CLEAR")
                c2.expect(r"  ✓ Released S4 \(old HCR port\)", since=m)
                clear = c2.lines(m)
                _relabel(c2, before[2], "S4")
        pm = s4.mark()
        time.sleep(12)
        assert qd not in s4.received(pm), "still polled after CLEAR"
        miss = _in_order(clear, HCR_RELEASE_S4)
        assert miss is None, f"CLEAR output lacks {miss!r}: {clear}"


def _lf(*frames):
    return b"".join(f.encode() + b"\n" for f in frames)


def _vol3(n):
    return _lf(f"<PVV{n}>", f"<PVA{n}>", f"<PVB{n}>")


STOP_BURST = _lf("<PSV,QT>", "<PSV,QPV>", "<PSA,QPA>", "<PSB,QPB>")


def _routed_verbs(bench, steps):
    """Run (;H command from W1, expected W2 S4 bytes) steps under FX-H4 -> (mismatches, RXERR, W2 [HCR] lines)."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    with _hcr_w2(bench) as c2:
        pm, cm = s4.mark(), c2.mark()
        bad = _steps(s4, w.send, steps)
        errs = s4.errors(pm)
        lines = [x.rstrip() for x in c2.lines(cm) if x.startswith("[HCR]")]
    return bad, errs, lines


@test("hcr.verbs_emote_raw", "Readable emote / muse / raw ;H verbs routed W1 -> W2 produce the exact HCR frames on W2 S4", needs=["wcb1"], links=["W2S4"])
def verbs_emote_raw(bench):
    bad, errs, lines = _routed_verbs(bench, [
        (";H,STIM,H,MOD", _lf("<SH0,QEH,QT>")), (";H,STIMULATE,M,STRONG", _lf("<SM1,QEM,QT>")),
        (";H,TRIGGER,S,1", _lf("<SS1,QES,QT>")), (";H,stim,c,0", _lf("<SC0,QEC,QT>")),
        (";H,OVERLOAD", _lf("<SE,QT>")), (";H,STOPEMOTE", _lf("<PSV,QT>")), (";H,RESETEMOTIONS", _lf("<OR,QE>")),
        (";H,STOP", STOP_BURST), (";H,SETEMOTION,H,50", _lf("<OH50,QEH>")), (";H,OVERRIDE,ON", _lf("<O1,QO>")),
        (";H,OVERRIDE,2", _lf("<O0,QO>")), (";H,MUSE", _lf("<MM>")), (";H,MUSE,GAP,5,30", _lf("<MN5,MX30>")),
        (";H,MUSE,ON", _lf("<M1,QM>")), (";H,MUSE,OFF", _lf("<M0,QM>")), (";H,RAW,<MM,QM>", _lf("<MM,QM>")),
        (";H,RAW,X", _lf("X")),
        # the word forms hcrEmotion / hcrLevel accept (WCB_HCR.cpp), and the NOW / GRACEFUL stop styles (hcrStopStyle):
        # StopGraceful = <PSG> then StopWAV(A) and StopWAV(B) (hcr.cpp:482-499); StopEmoteGraceful = <PSG> alone
        (";H,STIM,HAPPY,MODERATE", _lf("<SH0,QEH,QT>")), (";H,STIM,SAD,1", _lf("<SS1,QES,QT>")),
        (";H,STIM,MAD,STRONG", _lf("<SM1,QEM,QT>")), (";H,STIM,SCARED,0", _lf("<SC0,QEC,QT>")),
        (";H,STOP,NOW", STOP_BURST), (";H,STOP,GRACEFUL", _lf("<PSG>", "<PSA,QPA>", "<PSB,QPB>")),
        (";H,STOPEMOTE,G", _lf("<PSG>")), (";H,STOPEMOTE,HARD", _lf("<PSV,QT>")),
    ])
    assert not bad, "; ".join(bad)
    assert not errs, f"RXERR: {errs}"
    assert not lines, f"W2 printed HCR errors: {lines}"


@test("hcr.verbs_audio_volume", "PLAY/STOPWAV/VOL/VOLUP/VOLDN exact frames, the codec volume shadow and the 0-100 range", needs=["wcb1"], links=["W2S4"])
def verbs_audio_volume(bench):
    bad, errs, lines = _routed_verbs(bench, [
        (";H,PLAY,A,5", _lf("<CA0005,QPA>")), (";H,PLAY,B,9999", _lf("<CB9999,QPB>")), (";H,STOPWAV,B", _lf("<PSB,QPB>")),
        (";H,VOL,A,40", _lf("<PVA40>")), (";H,VOL,40", _vol3(40)), (";H,VOL,100", _vol3(100)), (";H,VOL,50", _vol3(50)),
        (";H,VOLUP", _vol3(55)), (";H,VOLDN,A,10", _lf("<PVA45>")), (";H,VOLUP,7", _lf("<PVV62>", "<PVA52>", "<PVB62>")),
        (";H,VOLDOWN,B", _lf("<PVB57>")), (";H,VOL,3", _vol3(3)), (";H,VOLDN", _vol3(0)), (";H,VOLUP,0", _vol3(5)),
    ])
    assert not bad, "; ".join(bad)
    assert not errs, f"RXERR: {errs}"
    assert not lines, f"W2 printed HCR errors: {lines}"


@test("hcr.fn_codec", ";H,FN numeric convention: every accepted fn's exact bytes and the rejection messages", needs=["wcb1"], links=["W2S4"])
def fn_codec(bench):
    rejects = [";H,FN,4,4,0", ";H,FN,2,1,100", ";H,FN,1", ";H,FN,12", ";H,FN,10,2", ";H,FN,17,0,101", ";H,FN,14,3,5"]
    bad, errs, lines = _routed_verbs(bench, [
        (";H,FN,4,0,50", _lf("<SH50,QEH,QT>")), (";H,FN,3,2,1", _lf("<SM1,QEM,QT>")), (";H,FN,2,1,99", _lf("<OS99,QES>")),
        (";H,FN,5", _lf("<SE,QT>")), (";H,FN,6", _lf("<MM>")), (";H,FN,7,5,30", _lf("<MN5,MX30>")), (";H,FN,8", STOP_BURST),
        (";H,FN,9", _lf("<PSV,QT>")), (";H,FN,10,1", _lf("<O1,QO>")), (";H,FN,11", _lf("<OR,QE>")),
        (";H,FN,13,0,1", _lf("<M1,QM>")), (";H,FN,14,0,5", _lf("<CV0005,QPV>")), (";H,FN,16,2", _lf("<PSB,QPB>")),
        (";H,FN,17,1,42", _lf("<PVA42>")), (";H,FN,17,3,42", _vol3(42)), (";H,FN,18,0,0", _vol3(47)),
        (";H,FN,19,0,10", _vol3(37)), (";H,FN,17,0,100", _lf("<PVV100>")),
    ] + [(r, b"") for r in rejects])
    want = [f"[HCR] FN {x} rejected (unknown fn or out-of-range)" for x in ("4,4,0", "2,1,100", "1,0,0", "12,0,0", "10,2,0", "17,0,101")]
    want.append("[HCR] FN 14 channel 3 out of range (0-2)")
    assert not bad, "; ".join(bad)
    assert not errs, f"RXERR: {errs}"
    assert lines == want, f"W2 HCR lines {lines}, expected {want}"


@test("hcr.bad_args", "Malformed ;H verbs print their usage line and never touch the wire", needs=["wcb1"], links=["W2S4"])
def bad_args(bench):
    stims = [";H,STIM,O,MOD", ";H,STIM,X,MOD", ";H,SETEMOTION,H,101", ";H,PLAY,V,5", ";H,PLAY,A,10000", ";H,STOPWAV,V",
             ";H,VOL,101", ";H,VOL,A", ";H,FADEIN,V,2", ";H,FADEOUT,X,2", ";H,RAW", ";H", ";H,FOO",
             ";H,STOP,BOGUS", ";H,STOPEMOTE,X"]
    bad, errs, lines = _routed_verbs(bench, [(s, b"") for s in stims])
    play, vol = "[HCR] Usage: ;H,PLAY,<A|B>,<0-9999>[,FADEIN,<sec>]", "[HCR] Usage: ;H,VOL[,<V|A|B>],<0-100>"
    want = ["[HCR] STIM does not take OVERLOAD — use ;H,OVERLOAD", "[HCR] Usage: ;H,STIM,<H|S|M|C>,<MOD|STRONG>",
            "[HCR] Usage: ;H,SETEMOTION,<H|S|M|C>,<0-100>", play, play, "[HCR] Usage: ;H,STOPWAV,<A|B>", vol, vol,
            "[HCR] Usage: ;H,FADEIN,<A|B>,<sec>", "[HCR] Usage: ;H,FADEOUT,<A|B>,<sec>", "[HCR] RAW needs a payload",
            "[HCR] Empty ;H command", "[HCR] Unknown ;H command: FOO",
            "[HCR] Usage: ;H,STOP[,NOW|GRACEFUL]", "[HCR] Usage: ;H,STOPEMOTE[,NOW|GRACEFUL]"]
    assert not bad, "; ".join(bad)
    assert lines == want, f"W2 HCR lines {lines}, expected {want}"


@test("hcr.setemotion_100_not_silent", "(should) ;H,SETEMOTION,H,100 — inside the advertised 0-100 — is sent or rejected, never silently dropped", needs=["wcb1"], links=["W2S4"])
def setemotion_100_not_silent(bench):
    """Probable bug: WCB_HCR.cpp:311 and its usage string accept 0-100, but HCRVocalizer::SetEmotion drops v > 99
    without a word (hcr.cpp:432), so the command reports nothing and sends nothing."""
    bad, errs, lines = _routed_verbs(bench, [(";H,SETEMOTION,H,100", b"")])
    assert bad or lines, "SETEMOTION,H,100 wrote no bytes and printed nothing"


@test("hcr.input_validation_gaps", "(should) ;H,VOL,X,40, ;H,FN,260,0,50 and ;H,FN,14,1,12345 are rejected, not muting, aliasing or sending a 5-digit track", needs=["wcb1"], links=["W2S4"])
def input_validation_gaps(bench):
    """Three gaps in WCB_HCR.cpp: a non-channel letter is taken as the value and toInt() gives 0, muting V/A/B
    (370-386); FN is cast with (uint8_t), so 256+n aliases fn n (264, 279); FN 14 calls PlayWAV directly and skips
    HcrCodec's 0-9999 track check (273-278)."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    got = {}
    with _hcr_w2(bench):
        prep = _steps(s4, w.send, [(";H,VOL,50", _vol3(50))])
        for cmd in (";H,VOL,X,40", ";H,FN,260,0,50", ";H,FN,14,1,12345"):
            m = s4.mark()
            w.send(cmd)
            time.sleep(0.8)
            got[cmd] = s4.received(m)
    assert not prep, prep
    sent = {k: v for k, v in got.items() if v}
    assert not sent, f"accepted and written: {sent}"


@test("hcr.play_debounce", "Two channel-A plays in one W2 loop pass: the second falls inside the 150 ms PlayWAV debounce; a later one plays", needs=["wcb1"], links=["W2S4"])
def play_debounce(bench):
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    with _hcr_w2(bench):
        time.sleep(0.4)
        m = s4.mark()
        # ?MGMT,FRAG runs the whole chain on W2 in one pass (WCB.ino:2910-2947); a plain ^ line on W1 would be
        # split into separate ETM unicasts with no guaranteed sub-150 ms spacing.
        w.send(f"?MGMT,FRAG,2,{nonce()},0,1,;H,PLAY,A,1^;H,PLAY,A,2^;H,PLAY,B,3")
        s4.expect(_lf("<CA0001,QPA>"), timeout=3, since=m)
        time.sleep(1.0)
        chain = s4.received(m)
        m = s4.mark()
        w.send(";H,PLAY,A,2")
        time.sleep(0.8)
        later = s4.received(m)
    assert chain == _lf("<CA0001,QPA>", "<CB0003,QPB>"), f"chain wrote {chain!r}"
    assert later == _lf("<CA0002,QPA>"), f"the later play wrote {later!r}"


def _timed_frames(l, since):
    """[(probe ms of the burst holding the frame's first byte, frame text without LF)] since the mark."""
    stream, starts = b"", []
    for ms, chunk in l.bursts(since):
        starts.append((len(stream), ms))
        stream += chunk
    out, pos = [], 0
    for part in stream.split(b"\n")[:-1]:
        out.append(([ms for s, ms in starts if s <= pos][-1], part.decode(errors="replace")))
        pos += len(part) + 1
    return out


@test("hcr.fade_instant_and_timed", "FADEOUT/FADEIN: exact instant forms, timed-ramp shape and end burst, restore point", needs=["wcb1"], links=["W2S4"])
def fade_instant_and_timed(bench):
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    with _hcr_w2(bench):
        bad = _steps(s4, w.send, [(";H,VOL,A,50", _lf("<PVA50>")), (";H,FADEOUT,A,0", _lf("<PVA0>", "<PSA,QPA>", "<PVA50>")),
                                  (";H,VOL,B,50", _lf("<PVB50>")), (";H,FADEIN,B,0", _lf("<PVB50>"))])
        m = s4.mark()
        w.send(";H,FADEOUT,B,1")
        time.sleep(2.0)
        frames = _timed_frames(s4, m)
        bad += _steps(s4, w.send, [(";H,VOLUP,B", _lf("<PVB55>"))])
    assert not bad, "; ".join(bad)
    texts = [t for _, t in frames]
    bench.note(f"FADEOUT,B,1 frames: {frames}")
    assert texts[-3:] == ["<PVB0>", "<PSB,QPB>", "<PVB50>"], f"the fade did not end with the stop burst: {texts}"
    steps = frames[:-3]
    vals = [int(x.group(1)) for x in (re.match(r"<PVB(\d+)>$", t) for _, t in steps) if x]
    assert len(vals) == len(steps) and 3 <= len(steps) <= 7, f"ramp frames {texts}"
    assert all(1 <= v <= 49 for v in vals) and all(a > b for a, b in zip(vals, vals[1:])), f"ramp values {vals}"
    assert all(b[0] - a[0] >= 140 for a, b in zip(steps, steps[1:])), f"ramp steps closer than 140 ms: {steps}"
    assert 750 <= frames[-3][0] - steps[0][0] <= 1100, f"end burst {frames[-3][0] - steps[0][0]} ms after the first step"


@test("hcr.play_fadein_and_cancel", "PLAY..FADEIN ramps to the old volume in ~2 s; VOL and STOPWAV cancel a fade in flight", needs=["wcb1"], links=["W2S4"])
def play_fadein_and_cancel(bench):
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    problems = []
    with _hcr_w2(bench):
        problems += _steps(s4, w.send, [(";H,VOL,A,60", _lf("<PVA60>"))])
        m = s4.mark()
        w.send(";H,PLAY,A,7,FADEIN,2")
        time.sleep(3.0)
        fade = _timed_frames(s4, m)
        problems += _steps(s4, w.send, [(";H,VOL,A,50", _lf("<PVA50>"))])
        w.send(";H,FADEOUT,A,3")
        time.sleep(0.6)
        m = s4.mark()
        w.send(";H,VOL,A,30")
        try:
            s4.expect(_lf("<PVA30>"), timeout=2, since=m)
        except AssertionError:
            problems.append("VOL,A,30 mid-fade wrote no <PVA30>")
        m = s4.mark()
        time.sleep(4.0)
        if s4.received(m):
            problems.append(f"the fade kept running after VOL: {s4.received(m)!r}")
        problems += _steps(s4, w.send, [(";H,VOL,A,40", _lf("<PVA40>"))])
        m0 = s4.mark()
        w.send(";H,FADEOUT,A,2")
        time.sleep(0.5)
        before_stop = s4.received(m0)
        m = s4.mark()
        w.send(";H,STOPWAV,A")
        try:
            s4.expect(_lf("<PSA,QPA>"), timeout=2, since=m)
        except AssertionError:
            problems.append("STOPWAV,A mid-fade wrote no <PSA,QPA>")
        time.sleep(0.3)
        m = s4.mark()
        time.sleep(3.0)
        if s4.received(m):
            problems.append(f"the fade kept running after STOPWAV: {s4.received(m)!r}")
        m = s4.mark()
        w.send(";H,VOLUP,A")
        time.sleep(0.8)
        bench.note(f"VOLUP after STOPWAV mid-fade (ramp so far {before_stop!r}) wrote {s4.received(m)!r}")
    texts = [t for _, t in fade]
    # The second <PVA0> is HcrFade re-anchoring after the library's own SetVolume (WCB_HCR.cpp:349-351) — harmless.
    head = 3 if texts[2:3] == ["<PVA0>"] else 2
    if texts[:2] != ["<PVA0>", "<CA0007,QPA>"]:
        problems.append(f"FADEIN start {texts[:3]}")
    ramp = [int(x.group(1)) for x in (re.match(r"<PVA(\d+)>$", t) for t in texts[head:]) if x]
    if len(ramp) != len(texts) - head or not ramp or ramp[-1] != 60 or any(a >= b for a, b in zip(ramp, ramp[1:])):
        problems.append(f"FADEIN ramp {texts[head:]}")
    elif not 1900 <= fade[-1][0] - fade[0][0] <= 2400:
        problems.append(f"final <PVA60> {fade[-1][0] - fade[0][0]} ms after the first byte")
    assert not problems, "; ".join(problems)


@test("hcr.stop_cancels_fade", "(should) ;H,STOP cancels an HcrFade in flight, like STOPWAV and VOL do", needs=["wcb1"], links=["W2S4"])
def stop_cancels_fade(bench):
    """Probable bug: WCB_HCR.cpp:306 only calls HCRVocalizer::Stop, so a FADEOUT started before STOP still ends ~dur
    later with a second StopWAV and a volume restore, and a FADEIN keeps raising the volume (STOPWAV and VOL cancel,
    lines 361 and 383)."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    with _hcr_w2(bench):
        prep = _steps(s4, w.send, [(";H,VOL,A,30", _lf("<PVA30>"))])
        m = s4.mark()
        w.send(";H,FADEOUT,A,1")
        w.send(";H,STOP")
        time.sleep(2.5)
        stream = s4.received(m)
    assert not prep, prep
    i = stream.find(STOP_BURST)
    assert i >= 0, f"no STOP burst: {stream!r}"
    tail = stream[i + len(STOP_BURST):]
    assert b"<PVA" not in tail and b"<PSA,QPA>" not in tail, f"the fade continued after STOP: {tail!r}"


@test("hcr.poll_refresh", "?HCR,POLL cadence, bounds, OFF and REFRESH; the POLL value round-trips into the config (~35 s)", needs=["wcb1"], links=["W2S4"])
def poll_refresh(bench):
    s4 = link(bench, 2, "S4")
    qd = POLL
    with _hcr_w2(bench) as c2:
        pm = s4.mark()
        cm = c2.send("?HCR,POLL,3")
        c2.expect(r"\[HCR\] Poll interval = 3s", since=cm)
        s4.expect(qd, timeout=1.0, since=pm)
        time.sleep(13)
        stream, times = _frame_times(s4, pm, qd)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert stream == qd * len(times) and len(times) >= 5, f"POLL,3 wrote {stream!r}"
        assert all(2950 <= g <= 3300 for g in gaps), f"poll gaps {gaps} (expected 3000 +0..+200)"
        assert "?HCR,POLL,3" in snapshot(bench, 2), "W2 config lacks ?HCR,POLL,3"
        for bad in ("2", "3601"):
            assert _has(_run(c2, f"?HCR,POLL,{bad}"), "[HCR] POLL must be 3-3600 s, or OFF (min 3s keeps any serial port safe)")
        pm = s4.mark()
        time.sleep(3.5)
        assert qd in s4.received(pm), "the 3 s cadence stopped after a rejected POLL"
        cm = c2.send("?HCR,POLL,OFF")
        c2.expect(r"\[HCR\] Poll interval = 0s \(off\)", since=cm)
        time.sleep(0.3)
        pm = s4.mark()
        time.sleep(8)
        assert not s4.received(pm), f"polled with POLL,OFF: {s4.received(pm)!r}"
        assert _has(_run(c2, "?HCR,LIST"), "  Poll:  0s (off)")
        pm = s4.mark()
        assert _has(_run(c2, "?HCR,REFRESH", 1.0), "[HCR] Refresh requested")
        assert s4.received(pm) == qd, f"REFRESH wrote {s4.received(pm)!r}"
        assert "?HCR,POLL,0" in snapshot(bench, 2), "W2 config lacks ?HCR,POLL,0"


@test("hcr.status_reply_parse", "Probe plays HCR replies to the one-frame poll: DF status, QVA and QEH are parsed into ?HCR,STATUS/GET; STATUS never transmits", needs=["wcb1"], links=["W2S4"])
def status_reply_parse(bench):
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    probe = s4.probe
    problems = []
    with _hcr_w2(bench) as c2:
        try:
            # Replies wait 50 ms: W2's soft-serial TX is critical-sectioned per byte and would clobber an overlapping
            # reply's RX edges. Bodies stay under the 32-byte parser buffer (hcr.h:26).
            s4.rule(1, POLL_HEX, b"<DF,1,2,3,4,1,1,9,2.5,0,5,0>\n<QVA,42>\n", delay_ms=50)
            s4.rule(2, b"<QM,QVA>\n".hex().upper(), b"<QVA,42>\n", delay_ms=50)
            pm = s4.mark()
            if not _has(_run(c2, "?HCR,REFRESH", 1.0), "[HCR] Refresh requested"):
                problems.append("REFRESH printed nothing")
            if not probe.rule_hits(1, pm):
                problems.append("the poll reply rule never fired")
            want = "[HCR:cfg=1,port=4,poll=0,age=*,H=1,S=2,M=3,C=4,dur=2.50,ovr=1,muse=1,wav=9,pV=0,pA=5,pB=0,vV=0,vA={},vB=0,rx=2,vage=-1]"   # rx: the DF + QVA replies; vage -1: V and B never confirmed (WCB_HCR.cpp printHCRStatus)
            time.sleep(0.5)      # the replies trail the poll by ~50 ms + transmit time
            va = 42              # the poll itself carries QVA, so the value is there on the first STATUS
            pm = s4.mark()
            got = next((x for x in _run(c2, "?HCR,STATUS", 1.0) if x.startswith("[HCR:cfg=")), "")
            bench.note(f"STATUS: {got}")
            # \b: the wildcard must not also swallow the vage= field
            if re.sub(r"\bage=-?\d+", "age=*", got) != want.format(va):
                problems.append(f"STATUS {got!r}, expected {want.format(va)!r} (age not compared)")
            sent = [t for _, t in _timed_frames(s4, pm)]
            if sent:     # a status reader must never transmit (rule 13): its query would mask the reply
                problems.append(f"STATUS transmitted {sent}")
            gets = [("VOL,A", "[HCR] VOL VOL,A = 42"), ("EMOTION,M", "[HCR] EMOTION EMOTION,M = 3"),
                    ("DURATION", "[HCR] DURATION = 2.50"), ("OVERRIDE", "[HCR] OVERRIDE = 1"), ("MUSE", "[HCR] MUSE = 1"),
                    ("WAVCOUNT", "[HCR] WAVCOUNT = 9"), ("PLAYING,A", "[HCR] PLAYING PLAYING,A = 5"),
                    ("VOL,X", "[HCR] VOL VOL,X = -1"),
                    ("FOO", "[HCR] GET fields: EMOTION,H|S|M|C / DURATION / OVERRIDE / MUSE / WAVCOUNT / PLAYING,V|A|B / VOL,V|A|B")]
            for field, line in gets:
                pm = s4.mark()
                out = _run(c2, f"?HCR,GET,{field}")
                if line not in out:
                    problems.append(f"GET,{field}: {out}")
                sent = [t for _, t in _timed_frames(s4, pm)]
                if field in ("VOL,A", "VOL,X") and sent != (["<QM,QVA>"] if field == "VOL,A" else []):
                    problems.append(f"GET,{field} queried {sent}")
            s4.send(b"<QEH,77>\n")
            time.sleep(0.5)
            if "[HCR] EMOTION EMOTION,H = 77" not in _run(c2, "?HCR,GET,EMOTION,H"):
                problems.append("the injected <QEH,77> was not parsed")
            problems += _steps(s4, w.send, [(";H,MUSE,TOGGLE", _lf("<M0,QM>"))])
        finally:
            probe.rule_del(1)
            probe.rule_del(2)
    assert not problems, "; ".join(problems)


@test("hcr.debug_periodic", "?DEBUG,HCR prints periodic status and per-command lines without adding wire traffic; OFF stops both (~25 s)", needs=["wcb1"], links=["W2S4"])
def debug_periodic(bench):
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    with _hcr_w2(bench, poll="3") as c2:
        try:
            pm = s4.mark()
            cm = c2.send("?DEBUG,HCR,ON")
            c2.expect(r"HCR debugging enabled", since=cm)
            time.sleep(10)
            dumps = [x for x in c2.lines(cm) if x.startswith("[HCR-DBG] status [HCR:cfg=1,port=4,poll=3,age=")]
            sent = [t for _, t in _timed_frames(s4, pm)]
            m, cm2 = s4.mark(), c2.mark()
            w.send(";H,VOL,A,40")
            s4.expect(_lf("<PVA40>"), timeout=2, since=m)
            time.sleep(0.5)
            cmd_lines = c2.lines(cm2)
        finally:
            c2.expect(r"HCR debugging disabled", since=c2.send("?DEBUG,HCR,OFF"))
        time.sleep(1.0)
        pm, cm = s4.mark(), c2.mark()
        time.sleep(7)
        quiet = [t for _, t in _timed_frames(s4, pm)]
        dbg_after = [x for x in c2.lines(cm) if x.startswith("[HCR-DBG]")]
    # The first dump may lag up to one interval: _dbgNext is a function static (WCB_HCR.cpp:170).
    assert 2 <= len(dumps) <= 5, f"{len(dumps)} status dumps in 10 s at poll=3"
    poll = POLL.decode().rstrip("\n")
    assert sent and set(sent) == {poll}, f"debug-on traffic {sent} (only the poll expected)"
    assert _has(cmd_lines, "[HCR-DBG] cmd in: ;H,VOL,A,40") and _has(cmd_lines, "[HCR-DBG] VOL ch=1 -> 40"), cmd_lines
    assert quiet and set(quiet) == {poll}, f"after debug off the wire carried {quiet}"
    assert not dbg_after, f"debug lines after OFF: {dbg_after}"


@test("hcr.config_rejects", "?HCR config validation on W2: the soft-serial 115200 block, bad baud/port/format, occupied ports, bad host; nothing changes", needs=["wcb1"], links=[])
def hcr_config_rejects(bench):
    if _device_tokens(bench, 2):
        raise Skip("W2 has HCR/MP3/DFP config")
    have = {t.upper() for t in bench.config_tokens(2)}
    host = "[HCR] Invalid host. Use ?HCR,REMOTE,W<n> (1-20, not this board)"
    checks = [
        ("?HCR,PORT,S4:115200", ["S4 is SOFTWARE SERIAL — 115200 baud is not received reliably (measured exact through 57600)",
                                 "❌ CONFIGURATION BLOCKED!", "  Use a hardware port: ?HCR,PORT,S1:115200   or   S4:57600"]),
        ("?HCR,PORT,S4:4800", ["[HCR] Baud must be 9600/19200/38400/57600/115200"]),
        ("?HCR,PORT,S6:9600", ["[HCR] Invalid serial port. Must be S1-S5"]),
        ("?HCR,PORT,S4", ["[HCR] Missing baud. Use: ?HCR,PORT,S<port>:<baud>"]),
        ("?HCR,FOO", ["[HCR] Invalid. Use: ?HCR,PORT,S<port>:<baud>  (e.g. S1:9600)"]),
        ("?HCR,REMOTE,W2", [host]), ("?HCR,REMOTE,W21", [host]),
        ("?HCR,GET,MUSE", ["[HCR] Not configured"]), ("?HCR,REFRESH", ["[HCR] Not configured"]),
    ]
    # Only send the S1/S2 lines when the owner that blocks them is there: an accepted ?HCR,PORT,S1 would take over
    # the real Maestro line, and its CLEAR would force the port to 9600.
    if "?WLED,1:W2S2:115200" in have:
        checks.append(("?HCR,PORT,S2:115200", ["[HCR] S2 already in use by PWM/Kyber/MP3/WLED/DFP - config blocked"]))
    if "?MAESTRO,REMOTE" in have:
        checks.append(("?HCR,PORT,S1:115200", ["[HCR] S1 already in use by PWM/Kyber/MP3/WLED/DFP - config blocked"]))
    bad = []
    with config_guard(bench, 2), Console(bench, 2) as c2:
        for cmd, wants in checks:
            out = _run(c2, cmd)
            miss = [x for x in wants if not _has(out, x)]
            if miss:
                bad.append(f"{cmd}: lacks {miss} in {out}")
    assert not bad, "; ".join(bad)


@test("hcr.port_isolation_broadcast", "HCR port: its RX is never run as commands, and broadcasts skip it even with broadcast output switched back on", needs=["wcb1"], links=["W2S3", "W2S4", "W2S5"])
def port_isolation_broadcast(bench):
    s3, s4, s5 = link(bench, 2, "S3"), link(bench, 2, "S4"), link(bench, 2, "S5")
    require_tokens(bench, 2, "?BCAST,IN,S3,ON", "?BCAST,OUT,S5,ON")
    # Plain lines injected here are ETM broadcasts too and reach every broadcast port on both WCBs, so they are
    # lowercase non-command markers.
    base = f"hilbc{nonce().lower()}".encode()
    watch = Watch(s3, s4, s5)
    s3.send(base + b"\r")
    watch.expect(s4, base + b"\r")
    watch.expect(s5, base + b"\r")
    assert base not in watch.got(s3), "the source port echoed its own broadcast"
    with _hcr_w2(bench) as c2:
        # Re-open S4's output, so only the HCR-port skip (WCB.ino:6983) can stop the write.
        c2.expect(r"Broadcast OUTPUT on S4: Enabled", since=c2.send("?BCAST,OUT,S4,ON"))
        mark_b = f"hilbc{nonce().lower()}".encode()
        watch = Watch(s3, s4, s5)
        s3.send(mark_b + b"\r")
        watch.expect(s5, mark_b + b"\r", timeout=3)
        time.sleep(1.5)
        got4, got3 = watch.got(s4), watch.got(s3)
        cm = c2.mark()
        watch = Watch(s5)
        s4.send(b";s5hil12\r")
        time.sleep(1.5)
        ran = watch.got(s5)
        printed = [x for x in c2.lines(cm) if x.strip()]
    assert mark_b not in got4, f"a broadcast reached the HCR port with its output enabled: {got4!r}"
    assert mark_b not in got3, "the source port echoed its own broadcast"
    assert b"hil12" not in ran and not printed, f"the HCR port's RX ran as a command: S5 {ran!r}, console {printed}"


@test("hcr.softserial_integrity_mesh_load", "Rule-13 regression: 300 HCR frames on soft-serial W2 S5 arrive intact under ~1800 mesh commands (slow, ~85 s)", needs=["wcb1"], links=["W2S5", "W2S2"])
def softserial_integrity_mesh_load(bench):
    """CLAUDE.md rule 13: with bit-banged TX an ESP-NOW interrupt mid-byte compressed the remaining bits, so the
    receiver saw a PREFIX of the command. S3-S5 now transmit through RMT (WCB_SoftSerial.h), so the timing no longer
    depends on the CPU at all; this keeps the original load as the regression bar."""
    s5, s2 = link(bench, 2, "S5"), link(bench, 2, "S2")
    require_tokens(bench, 2, "?WLED,1:W2S2:115200")   # W2 S2 is WLED 1's port; there is no real WLED on the bench
    w = usb_wcb(bench)
    with _hcr_w2(bench, port="S5", debug=True) as c2:
        protected = _has(c2.config_lines, "[SOFTSERIAL] S5 TX: RMT (hardware-timed)")
        s5.listen(9600, hw=True)   # a hardware probe channel, so a probe-side soft-RX error cannot pass for a WCB mis-frame
        pm5, pm2 = s5.mark(), s2.mark()
        sent, n, next_h = [], 0, time.monotonic()
        while n < 300:
            if time.monotonic() >= next_h:
                w.send(f";H,RAW,<CA{n:04d},QPA>")
                n += 1
                next_h += 0.25
            t = "N" + nonce()      # a letter first, so the text can never read as part of the ;S port number
            sent.append(t)
            w.send(f";W2,;S2{t}")
            time.sleep(0.05)
        time.sleep(3.0)
        frames = [t for _, t in _timed_frames(s5, pm5)]
        errs5, errs2, got2 = s5.errors(pm5), s2.errors(pm2), s2.received(pm2)
    want = [f"<CA{i:04d},QPA>" for i in range(300)]
    lost = [t for t in sent if f"{t}\r".encode() not in got2]
    wrong = [(i, g) for i, (g, x) in enumerate(zip(frames, want)) if g != x]
    bench.note(f"soft-serial load: {len(frames)} frames, {len(wrong)} wrong, RXERR S5 {len(errs5)}, S2 lost {len(lost)}/{len(sent)}")
    assert protected, "configuring HCR on S5 did not report RMT TX for the port"
    assert frames == want, f"{len(frames)} frames on S5, first differences {wrong[:5]}"
    assert not errs5, f"RXERR on S5: {errs5[:5]}"
    assert not lost and not errs2, f"W2 S2 lost {len(lost)} of {len(sent)} markers, RXERR {errs2[:5]}"


# ============================================================ routing and WDP learning
@test("hcr.w1_hw115200_propagation", "HCR on W1 hardware S2 at 115200: local bytes; W2 persists a learned route; W1's own route is wiped; full restore", needs=["wcb1"])
def w1_hw115200_propagation(bench):
    s2 = link(bench, 1, "S2")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    _require_free(bench, 1, "S2")
    if _device_tokens(bench, 2):
        raise Skip("W2 has HCR/MP3/DFP config")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 1) as c1, Console(bench, 2) as c2:
        configured = False
        try:
            c1.expect(r"\[HCR\] Poll interval = 0s \(off\)", since=c1.send("?HCR,POLL,OFF"))
            cm2 = c2.mark()
            m = c1.send("?HCR,PORT,S2:115200")
            configured = True
            c1.expect(r"\[HCR\] Configured on S2 at 115200 baud", since=m)
            miss = _in_order(c1.lines(m), ["Baud rate for Serial2 updated to 115200", "  ⚠️  Disabled broadcast output on S2 (HCR port)",
                                           "  ⚠️  Disabled broadcast input on S2 (HCR port)", "Serial2 label set to: 'HCR'",
                                           "[HCR] Active on S2 at 115200 baud, poll=0s", "[HCR] Configured on S2 at 115200 baud"])
            if miss:
                problems.append(f"config output lacks {miss!r}")
            try:
                c2.expect(r"\[WDP\] HCR host learned — routing ;H to WCB1", timeout=4, since=cm2)
            except AssertionError:
                problems.append("W2 did not learn W1 as the HCR host")
            tokens = snapshot(bench, 1)
            if "?HCR,REMOTE,W2" in tokens:
                problems.append("configuring a local HCR kept W1's remote route (WCB_HCR.cpp:516 zeroes it)")
            problems += [f"W1 config lacks {t}" for t in ("?HCR,PORT,S2:115200", "?HCR,POLL,0", "?BAUD,S2,115200",
                                                          "?LABEL,S2,HCR", "?BCAST,OUT,S2,OFF", "?BCAST,IN,S2,OFF") if t not in tokens]
            if "?HCR,REMOTE,W1" not in snapshot(bench, 2):
                problems.append("W2 config lacks the learned ?HCR,REMOTE,W1")
            s2.listen(115200)
            problems += _steps(s2, w.send, [(";H,STIM,H,STRONG", _lf("<SH1,QEH,QT>"))])
        finally:
            if configured:
                m = c1.send("?HCR,REMOTE,W2")      # clears the local HCR first, then routes to W2 again
                c1.expect(r"\[HCR\] Routing ;H to WCB2", since=m)
                _relabel(c1, before[1], "S2")
                s2.listen()
                _unlearn(bench, "HCR", learner=2)
    assert not problems, "; ".join(problems)


@test("wdp.hcr_relearn_is_restore", "W1's HCR route cleared; W2 starts hosting HCR; W1 re-learns W2 (the baseline) and ;H lands on W2 S4; DUMP CAP bit 0x0001", needs=["wcb1"])
def hcr_relearn_is_restore(bench):
    s4 = link(bench, 2, "S4")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip("W2 has HCR/MP3/DFP config")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        configured = False
        try:
            assert _has(w.run("?HCR,REMOTE,OFF"), "[HCR] Remote routing cleared")
            pm = s4.mark()
            if not _has(w.run(";H,OVERLOAD"), "[HCR] Not configured — use ?HCR,PORT,Sx:baud first"):
                problems.append("with no route and no host, W1 did not report HCR not configured")
            time.sleep(0.5)
            if s4.received(pm):
                problems.append(f"bytes reached W2 S4 with no route: {s4.received(pm)!r}")
            c2.expect(r"\[HCR\] Poll interval = 0s", since=c2.send("?HCR,POLL,OFF"))
            wm = w.dev.mark()
            m = c2.send("?HCR,PORT,S4:9600")
            configured = True
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=m)
            try:
                w.dev.expect(r"\[WDP\] HCR host learned — routing ;H to WCB2", timeout=5, since=wm)
            except AssertionError:
                problems.append("W1 did not learn W2 as the HCR host")
            if not _has(w.run("?HCR,LIST"), "  Routes ;H to WCB2 (remote host)"):
                problems.append("?HCR,LIST does not route to WCB2")
            row = next((x for x in w.run("?WDP,DUMP", timeout=8) if x.startswith("[WDP:N=2,")), "")
            cap = re.search(r"CAP=([0-9A-Fa-f]+)", row)
            if not cap or not int(cap.group(1), 16) & 0x0001:
                problems.append(f"W2's DUMP row lacks the HCR cap bit: {row}")
            problems += _steps(s4, w.send, [(";H,OVERLOAD", _lf("<SE,QT>"))])
            if "?HCR,REMOTE,W2" not in snapshot(bench, 1):
                problems.append("the re-learn did not restore W1's ?HCR,REMOTE,W2")
        finally:
            if configured:
                m = c2.send("?HCR,CLEAR")
                c2.expect(r"  ✓ Released S4 \(old HCR port\)", since=m)
                _relabel(c2, before[2], "S4")
            if "?HCR,REMOTE,W2" not in snapshot(bench, 1):
                w.run("?HCR,REMOTE,W2")
    assert not problems, "; ".join(problems)


@test("route.live_election_autojoin_off", "With auto-join off and no stored route, ;H routes to W2 by live capability election and nothing is persisted", needs=["wcb1"])
def live_election_autojoin_off(bench):
    s4 = link(bench, 2, "S4")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    if "?WDP,AUTOJOIN,OFF" in bench.config_tokens(1):
        raise Skip("W1 already has auto-join off")
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip("W2 has HCR/MP3/DFP config")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        configured = False
        try:
            assert _has(w.run("?WDP,AUTOJOIN,OFF"), "[WDP] auto-join disabled")
            assert _has(w.run("?HCR,REMOTE,OFF"), "[HCR] Remote routing cleared")
            c2.expect(r"\[HCR\] Poll interval = 0s", since=c2.send("?HCR,POLL,OFF"))
            wm = w.dev.mark()
            m = c2.send("?HCR,PORT,S4:9600")
            configured = True
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=m)
            assert _has(w.run("?WDP,POLL"), "[WDP] polled")
            time.sleep(2)
            w.run("?DEBUG,ETM,ON")
            rm = w.dev.mark()
            problems += _steps(s4, w.send, [(";H,MUSE", _lf("<MM>"))])
            if not _has(w.dev.since(rm), "[ROUTE] ;H,MUSE -> host WCB2"):
                problems.append("W1 printed no [ROUTE] line to WCB2")
            time.sleep(3)
            if _has(w.dev.since(wm), "HCR host learned"):
                problems.append("W1 learned an HCR host with auto-join off")
            tokens = snapshot(bench, 1)
            if [t for t in tokens if t.upper().startswith("?HCR,REMOTE")]:
                problems.append("the election persisted an ?HCR,REMOTE route")
            if "?WDP,AUTOJOIN,OFF" not in tokens:
                problems.append("W1 config lacks ?WDP,AUTOJOIN,OFF")
        finally:
            w.run("?DEBUG,ETM,OFF")
            if configured:
                m = c2.send("?HCR,CLEAR")
                c2.expect(r"  ✓ Released S4 \(old HCR port\)", since=m)
                _relabel(c2, before[2], "S4")
            w.run("?HCR,REMOTE,W2")      # before auto-join returns, so no advert can race it
            w.run("?WDP,AUTOJOIN,ON")
    assert not problems, "; ".join(problems)


# ============================================================ MP3 Trigger
def _rejects(bench, wcb, checks):
    """Run (command, [expected substrings]) on W<wcb>'s console under config_guard; nothing may change."""
    bad = []
    with config_guard(bench, wcb), Console(bench, wcb) as c:
        for cmd, wants in checks:
            out = _run(c, cmd)
            miss = [x for x in wants if not _has(out, x)]
            if miss:
                bad.append(f"{cmd}: lacks {miss} in {out}")
    assert not bad, "; ".join(bad)


@test("mp3.config_bytes_propagation", "MP3 Trigger on W2 S5: config lines, W1 auto-learns, every ;A verb's exact bytes and volume line, rejects", needs=["wcb1"], links=["W2S5"])
def mp3_config_bytes_propagation(bench):
    s5 = link(bench, 2, "S5")
    w = usb_wcb(bench)
    problems = []
    with _w2_audio(bench, "MP3", "S5:9600:V20", "S5") as c2:
        miss = _in_order(c2.config_lines, ["  ⚠️  Disabled broadcast output on S5 (MP3 Trigger port)",
                                           "  ⚠️  Disabled broadcast input on S5 (MP3 Trigger port)",
                                           "Serial5 label set to: 'MP3 Trigger'", "[MP3] Configured: S5 at 9600 baud  default volume=20"])
        if miss:
            problems.append(f"config output lacks {miss!r}")
        lst = _run(c2, "?MP3,LIST")
        miss = _in_order(lst, ["--- MP3 Trigger Configuration ---", "  Port          : S5", "  Baud          : 9600",
                               "  Default Volume: 20  (0=loudest, 64=inaudible ceiling)", "  Current Volume: 20",
                               "  On Error      : (none)"])
        if miss:
            problems.append(f"W2 ?MP3,LIST lacks {miss!r}")
        if not _has(w.run("?MP3,LIST"), "  Routes ;A to WCB2 (remote host)"):
            problems.append("W1 ?MP3,LIST does not route to WCB2")
        cm = c2.mark()
        problems += _steps(s5, w.send, [(f";A,{verb}", bytes.fromhex(hexb)) for verb, hexb in (
            ("PLAY,5", "76147405"), ("PLAY,255", "761474FF"), ("PLAYFS,0", "76147000"), ("STOP", "4F"), ("NEXT", "46"),
            ("PREV", "52"), ("COUNT", "5331"), ("VER", "5330"), ("VOL,30", "761E"), ("PLAY,1", "761E7401"),
            ("VOLUP", "7619"), ("VOL,3", "7603"), ("VOLUP", "7600"), ("VOL,60", "763C"), ("VOLDN", "7640"),
            ("PLAY,0", ""), ("PLAY,256", ""), ("VOL,65", ""), ("FOO", ""))])
        lines = c2.lines(cm)
        vols = [int(x.split("→")[1]) for x in lines if x.startswith("[MP3] Volume → ")]
        if vols != [30, 25, 3, 0, 60, 64]:
            problems.append(f"volume lines {vols}")
        for r in ("PLAY,0", "PLAY,256", "VOL,65", "FOO"):
            if not _has(lines, f"[MP3] Unknown or invalid audio command: {r}"):
                problems.append(f"no rejection line for {r}")
    assert not problems, "; ".join(problems)


@test("mp3.rx_callbacks", "Probe plays MP3 Trigger replies: X fires ONFIN once, x cancels, E runs ONERR, = status, M swallows 3 bytes, port RX never runs", needs=["wcb1"], links=["W2S5", "W2S3"])
def mp3_rx_callbacks(bench):
    s5, s3 = link(bench, 2, "S5"), link(bench, 2, "S3")
    if [t for t in bench.config_tokens(2, refresh=True) if t.upper().startswith(("?SEQ,SAVE,HILK,", "?SEQ,SAVE,HILE,"))]:
        raise Skip("W2 already stores HILK/HILE")
    w = usb_wcb(bench)
    ok, err = f"hilok{nonce().lower()}", f"hilerr{nonce().lower()}"
    problems = []

    def inject(data, wait=0.8):
        cm, m3 = c2.mark(), s3.mark()
        s5.send(data)
        time.sleep(wait)
        return c2.lines(cm), s3.received(m3)

    with _w2_audio(bench, "MP3", "S5:9600:V20", "S5") as c2:
        try:
            # Recalled sequences run with local origin and could fan out mesh-wide, so they hold ;S3 markers only.
            _run(c2, f"?SEQ,SAVE,HILK,;S3{ok}")
            _run(c2, f"?SEQ,SAVE,HILE,;S3{err}")
            c2.expect(r"\[MP3\] Error callback → ;CHILE", since=c2.send("?MP3,ONERR,HILE"))
            problems += _steps(s5, w.send, [(";A,PLAY,5,ONFIN,HILK", bytes.fromhex("76147405"))])
            lines, got3 = inject(b"X")
            if not (_has(lines, "[MP3] Track finished") and _has(lines, f"Recalling command for key 'HILK': ;S3{ok}") and ok.encode() + b"\r" in got3):
                problems.append(f"first X: {lines} / S3 {got3!r}")
            lines, got3 = inject(b"X")
            if not _has(lines, "[MP3] Track finished") or _has(lines, "Recalling") or got3:
                problems.append(f"second X recalled again: {lines} / S3 {got3!r}")
            problems += _steps(s5, w.send, [(";A,PLAY,6,HILK", bytes.fromhex("76147406"))])
            lines, _ = inject(b"x", 0.4)
            if not _has(lines, "[MP3] Track cancelled"):
                problems.append(f"x: {lines}")
            lines, got3 = inject(b"X")
            if not _has(lines, "[MP3] Track finished") or _has(lines, "Recalling") or got3:
                problems.append(f"X after x recalled: {lines} / S3 {got3!r}")
            lines, got3 = inject(b"E")
            if not _has(lines, "[MP3] Error: track not found or device error") or err.encode() + b"\r" not in got3:
                problems.append(f"E: {lines} / S3 {got3!r}")
            lines, _ = inject(b"=V2.4\r")
            if not _has(lines, "[MP3] Status: V2.4"):
                problems.append(f"=V2.4: {lines}")
            lines, _ = inject(b"MXXX")
            if [x for x in lines if x.startswith("[MP3]")]:
                problems.append(f"M did not swallow its 3 bytes: {lines}")
            lines, got3 = inject(b";s3hil12\r", 1.5)
            if b"hil12" in got3 or _has(lines, "hil12"):
                problems.append(f"the MP3 port's RX ran as a command: {lines} / S3 {got3!r}")
            tokens = snapshot(bench, 2)
            i = tokens.index("?MP3,S5:9600:V20") if "?MP3,S5:9600:V20" in tokens else -1
            if i < 0 or tokens[i + 1:i + 2] != ["?MP3,ONERR,HILE"]:
                problems.append(f"config lacks ?MP3,S5:9600:V20^?MP3,ONERR,HILE: {[t for t in tokens if t.startswith('?MP3')]}")
            c2.expect(r"\[MP3\] Error callback cleared", since=c2.send("?MP3,ONERR,CLEAR"))
            if [t for t in snapshot(bench, 2) if t.startswith("?MP3,ONERR")]:
                problems.append("ONERR,CLEAR left the ONERR token")
        finally:
            for key in ("HILK", "HILE"):
                c2.expect(rf"Deleted stored command key: '{key}'", since=c2.send(f"?SEQ,CLEAR,{key}"))
    assert not problems, "; ".join(problems)


@test("mp3.config_rejects", "?MP3 validation on W2 (baud, format, volume, occupied port, host, ONERR length); nothing changes", needs=["wcb1"], links=[])
def mp3_config_rejects(bench):
    if _device_tokens(bench, 2, "MP3"):
        raise Skip("W2 has MP3 config")
    host = "[MP3] Invalid host. Use ?MP3,REMOTE,W<n> (1-20, not this board)"
    example = "  Example: ?MP3,S2:9600:V25"
    checks = [
        ("?MP3,S5:19200:V20", ["[MP3] MP3 Trigger only supports 9600 or 38400 baud"]),
        ("?MP3,S5:9600", ["[MP3] Incomplete config. Use: ?MP3,S<port>:<baud>:V<vol>", example]),
        ("?MP3,S5:9600:25", ["[MP3] Volume required. Use :V<0-64> at the end (e.g. :V25)"]),
        ("?MP3,S5:9600:V65", ["[MP3] Volume must be 0-64 (0=loudest, 64=inaudible)"]),
        ("?MP3,S6:9600:V20", ["[MP3] Invalid serial port. Must be S1-S5"]),
        ("?MP3,X", ["[MP3] Invalid format. Use: ?MP3,S<port>:<baud>:V<vol>", example]),
        ("?MP3,REMOTE,W2", [host]), ("?MP3,REMOTE,W21", [host]),
        # 24 chars: rejected. Never send a VALID key to an unconfigured board — it persists invisibly in NVS.
        ("?MP3,ONERR,ABCDEFGHIJKLMNOPQRSTUVWX", ["[MP3] ONERR: key must be 1-15 characters"]),
        ("?MP3,ONERR,ABCDEFGHIJKLMNOP", ["[MP3] ONERR: key must be 1-15 characters"]),   # a sequence key's limit
    ]
    if "?WLED,1:W2S2:115200" in bench.config_tokens(2):
        checks.append(("?MP3,S2:9600:V20", ["[MP3] S2 already in use by PWM/Kyber/HCR/WLED/DFP - config blocked"]))
    _rejects(bench, 2, checks)


@test("mp3.hw38400_one_hop", "MP3 on W1 hardware S2 at 38400; W2 learns W1; a mesh-received ;A on W2 runs locally and is never re-forwarded", needs=["wcb1"])
def mp3_hw38400_one_hop(bench):
    s2 = link(bench, 1, "S2")
    _require_free(bench, 1, "S2")
    if _device_tokens(bench, 1, "MP3", "DFP") or _device_tokens(bench, 2):
        raise Skip("W1 or W2 already has device config")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 1) as c1, Console(bench, 2) as c2:
        configured = False
        try:
            cm2 = c2.mark()
            m = c1.send("?MP3,S2:38400:V10")
            configured = True
            c1.expect(r"\[MP3\] Configured: S2 at 38400 baud  default volume=10", since=m)
            miss = _in_order(c1.lines(m), ["Baud rate for Serial2 updated to 38400", "  ⚠️  Disabled broadcast output on S2 (MP3 Trigger port)",
                                           "  ⚠️  Disabled broadcast input on S2 (MP3 Trigger port)", "Serial2 label set to: 'MP3 Trigger'",
                                           "[MP3] Configured: S2 at 38400 baud  default volume=10"])
            if miss:
                problems.append(f"config output lacks {miss!r}")
            try:
                c2.expect(r"\[WDP\] MP3 host learned — routing ;A to WCB1", timeout=4, since=cm2)
            except AssertionError:
                problems.append("W2 did not learn W1 as the MP3 host")
            s2.listen(38400)
            problems += _steps(s2, w.send, [(";A,PLAY,7", bytes.fromhex("760A7407"))])
            pm, cm = s2.mark(), c2.mark()
            w.send(";W2,;A,PLAY,7")
            try:
                c2.expect(r"\[MP3\] Not configured — use \?MP3,S<port>:<baud>:V<vol>", timeout=4, since=cm)
            except AssertionError:
                problems.append("W2 did not run the mesh-received ;A locally")
            time.sleep(2)
            if s2.received(pm):
                problems.append(f"the mesh-received ;A was forwarded back to W1: {s2.received(pm)!r}")
        finally:
            if configured:
                c1.expect(r"\[MP3\] Local configuration cleared", since=c1.send("?MP3,CLEAR"))
                time.sleep(0.5)
                _relabel(c1, before[1], "S2")
                s2.listen()
                _unlearn(bench, "MP3", learner=2)
    assert not problems, "; ".join(problems)


@test("mp3.volume_reboot_divergence", "Characterization: the current MP3 volume survives a W2 reboot while the config still reports the default (W2 reboot)", needs=["wcb1"], links=["W2S5"])
def mp3_volume_reboot_divergence(bench):
    """The live volume is NVS key 'vol' (WCB_MP3.cpp:439) but not in the backup, so config_guard cannot see it drift;
    the fixture's CLEAR resets it to 20."""
    s5 = link(bench, 2, "S5")
    w = usb_wcb(bench)
    with _w2_audio(bench, "MP3", "S5:9600:V20", "S5") as c2:
        problems = _steps(s5, w.send, [(";A,VOL,30", bytes.fromhex("761E"))])
        m = w.send(";W2,?reboot")
        w.dev.expect(r"^\[ETM\] WCB2 came ONLINE \(boot\)", timeout=25, since=m)
        time.sleep(3)
        lst = _run(c2, "?MP3,LIST")
        for want in ("  Default Volume: 20  (0=loudest, 64=inaudible ceiling)", "  Current Volume: 30"):
            if want not in lst:
                problems.append(f"after reboot ?MP3,LIST lacks {want!r}: {lst}")
        problems += _steps(s5, w.send, [(";A,PLAY,1", bytes.fromhex("761E7401"))])
        if "?MP3,S5:9600:V20" not in snapshot(bench, 2):
            problems.append("the config token changed")
    assert not problems, "; ".join(problems)


# ============================================================ DFPlayer Mini
def _dfp(cmd, param=0):
    """A DFPlayer frame: 7E FF 06 cmd 00 param16, checksum = two's complement of bytes 1-6, EF."""
    body = bytes([0xFF, 0x06, cmd, 0x00, param >> 8, param & 0xFF])
    c = -sum(body) & 0xFFFF
    return b"\x7e" + body + bytes([c >> 8, c & 0xFF, 0xEF])


def _no_recall_keys(bench):
    if [t for t in bench.config_tokens(2, refresh=True) if t.upper().startswith(("?SEQ,SAVE,HILK,", "?SEQ,SAVE,HILE,"))]:
        raise Skip("W2 already stores HILK/HILE")


@contextmanager
def _recall_keys(c2, kind, ok, err):
    """Stored sequences HILK/HILE plus ONERR -> HILE on W2; deleted on exit. Recalls run with local origin and could
    fan out mesh-wide, so they hold ;S3 markers only."""
    try:
        _run(c2, f"?SEQ,SAVE,HILK,;S3{ok}")
        _run(c2, f"?SEQ,SAVE,HILE,;S3{err}")
        c2.expect(rf"\[{kind}\] Error callback → ;CHILE", since=c2.send(f"?{kind},ONERR,HILE"))
        yield
    finally:
        for key in ("HILK", "HILE"):
            c2.expect(rf"Deleted stored command key: '{key}'", since=c2.send(f"?SEQ,CLEAR,{key}"))


def _inject(c2, src, s3, data, wait=0.8):
    """Probe writes `data` into the device port -> (W2 console lines, bytes on W2 S3) within `wait`."""
    cm, m3 = c2.mark(), s3.mark()
    src.send(data)
    time.sleep(wait)
    return c2.lines(cm), s3.received(m3)


DFP_FINISH_5 = _dfp(0x3D, 5)


@test("dfp.config_frames", "DFPlayer on W2 S5: config lines, W1 auto-learns, every ;D verb's exact 10-byte frame and diag line, rejects, re-config", needs=["wcb1"], links=["W2S5"])
def dfp_config_frames(bench):
    s5 = link(bench, 2, "S5")
    w = usb_wcb(bench)
    ok_verbs = [("PLAY,5", 0x03, 5), ("PLAY,2999", 0x03, 2999), ("FOLDER,1,5", 0x0F, 0x0105), ("FOLDER,99,255", 0x0F, 0x63FF),
                ("MP3FOLDER,9999", 0x12, 9999), ("STOP", 0x16, 0), ("NEXT", 0x01, 0), ("PREV", 0x02, 0), ("PAUSE", 0x0E, 0),
                ("RESUME", 0x0D, 0), ("RANDOM", 0x18, 0), ("RESET", 0x0C, 0), ("VOL,30", 0x06, 30), ("VOL,20", 0x06, 20),
                ("VOLUP", 0x06, 22), ("VOLDN", 0x06, 20), ("LOOP,5", 0x08, 5), ("LOOPALL,1", 0x11, 1), ("LOOPFOLDER,2", 0x17, 2),
                ("EQ,5", 0x07, 5), ("STATUS", 0x42, 0)]
    rejects = ["PLAY,0", "PLAY,3000", "FOLDER,100,1", "FOLDER,1,256", "FOLDER,1", "VOL,31", "LOOPALL,2", "EQ,6", "FOO"]
    problems = []
    with _w2_audio(bench, "DFP", "S5", "S5") as c2:
        miss = _in_order(c2.config_lines, ["  ⚠️  Disabled broadcast output on S5 (DFPlayer port)", "  ⚠️  Disabled broadcast input on S5 (DFPlayer port)",
                                           "Serial5 label set to: 'DFPlayer'", "[DFP] Configured: S5 at 9600 baud  default volume=20"])
        if miss:
            problems.append(f"config output lacks {miss!r}")
        miss = _in_order(_run(c2, "?DFP,LIST"), ["--- DFPlayer Mini Configuration ---", "  Port          : S5",
                                                 "  Baud          : 9600  (module-fixed)", "  Default Volume: 20  (0=silent, 30=loudest)",
                                                 "  Current Volume: 20", "  On Error      : (none)"])
        if miss:
            problems.append(f"W2 ?DFP,LIST lacks {miss!r}")
        if not _has(w.run("?DFP,LIST"), "  Routes ;D to WCB2 (remote host)"):
            problems.append("W1 ?DFP,LIST does not route to WCB2")
        cm = c2.mark()
        problems += _steps(s5, w.send, [(f";D,{v}", _dfp(c, p)) for v, c, p in ok_verbs] + [(f";D,{r}", b"") for r in rejects])
        lines = [x.rstrip() for x in c2.lines(cm)]
        diag = [x for x in lines if x.startswith("[DFP] cmd=0x")]
        want = [f"[DFP] cmd=0x{c:02X} param={p}" for _, c, p in ok_verbs]
        if diag != want:
            problems.append(f"diag lines {diag}")
        vols = [x for x in lines if x.startswith("[DFP] Volume → ")]
        if vols != [f"[DFP] Volume → {v}" for v in (30, 20, 22, 20)]:
            problems.append(f"volume lines {vols}")
        problems += [f"no rejection line for {r}" for r in rejects if f"[DFP] Unknown or invalid command: {r}" not in lines]
        m = c2.send("?DFP,S5:V25")
        c2.expect(r"\[DFP\] Configured: S5 at 9600 baud  default volume=25", since=m)
        time.sleep(0.3)
        again = c2.lines(m)
        if not _has(again, "Serial5 label set to: 'DFPlayer'") or _has(again, "Disabled broadcast"):
            problems.append(f"re-config on the same port printed {again}")
        if "?DFP,S5:9600:V25" not in snapshot(bench, 2):
            problems.append("config lacks ?DFP,S5:9600:V25")
    assert not problems, "; ".join(problems)


@test("dfp.device_verb", "(should) ;D,DEVICE,2 — advertised in the help — sends the select-device frame", needs=["wcb1"], links=["W2S5"])
def dfp_device_verb(bench):
    """Probable bug: processDFPCommand strips 'D,' (WCB_DFP.cpp:72-74), then DfPlayerCodec::handle strips an optional
    leading 'D' again (WcbDfPlayer.cpp:54), so DEVICE,2 becomes 'EVICE,2' and is rejected. ;D,DDEVICE,2 reaches it.
    WcbCmd is shared with NaviCore — check its call sites before fixing it there."""
    s5 = link(bench, 2, "S5")
    w = usb_wcb(bench)
    with _w2_audio(bench, "DFP", "S5", "S5") as c2:
        workaround = _steps(s5, w.send, [(";D,DDEVICE,2", _dfp(0x09, 2))])
        bench.note(f";D,DDEVICE,2 workaround: {workaround or 'sends the frame'}")
        bad = _steps(s5, w.send, [(";D,DEVICE,2", _dfp(0x09, 2))])
    assert not bad, "; ".join(bad)


@test("dfp.rx_frames", "Probe plays DFPlayer frames: finish fires ONFIN (PLAY and FOLDER forms), error runs ONERR, online, STOP cancels ONFIN, no checksum check", needs=["wcb1"], links=["W2S5", "W2S3"])
def dfp_rx_frames(bench):
    s5, s3 = link(bench, 2, "S5"), link(bench, 2, "S3")
    _no_recall_keys(bench)
    w = usb_wcb(bench)
    ok, err = f"hilok{nonce().lower()}", f"hilerr{nonce().lower()}"
    recall = f"Recalling command for key 'HILK': ;S3{ok}"
    problems = []
    with _w2_audio(bench, "DFP", "S5", "S5") as c2, _recall_keys(c2, "DFP", ok, err):
        try:
            def finish(expect_recall, label):
                time.sleep(0.15)   # keep 100 ms+ between an outgoing frame and an injection (soft-serial TX critical sections)
                lines, got3 = _inject(c2, s5, s3, DFP_FINISH_5)
                fired = _has(lines, recall) and ok.encode() + b"\r" in got3
                if not _has(lines, "[DFP] Track 5 finished") or fired != expect_recall:
                    problems.append(f"{label}: {lines} / S3 {got3!r}")

            problems += _steps(s5, w.send, [(";D,PLAY,5,ONFIN,HILK", _dfp(0x03, 5))])
            finish(True, "PLAY..ONFIN finish")
            lines, got3 = _inject(c2, s5, s3, _dfp(0x40, 6))
            if not _has(lines, "[DFP] Error 0x06") or err.encode() + b"\r" not in got3:
                problems.append(f"error frame: {lines} / S3 {got3!r}")
            lines, _ = _inject(c2, s5, s3, _dfp(0x3F, 2))
            if not _has(lines, "[DFP] Online (storage 0x02)"):
                problems.append(f"online frame: {lines}")
            problems += _steps(s5, w.send, [(";D,FOLDER,1,5,HILK", _dfp(0x0F, 0x0105))])
            finish(True, "FOLDER finish")
            problems += _steps(s5, w.send, [(";D,PLAY,5,HILK", _dfp(0x03, 5)), (";D,STOP", _dfp(0x16))])
            finish(False, "finish after STOP")
            problems += _steps(s5, w.send, [(";D,PLAY,5,HILK", _dfp(0x03, 5))])
            time.sleep(0.15)
            # Junk, then a USB-storage finish (0x3C) with a zero checksum: the codec never checks it.
            lines, got3 = _inject(c2, s5, s3, bytes.fromhex("0011227EFF063C0000050000EF"))
            if not _has(lines, "[DFP] Track 5 finished") or not _has(lines, recall):
                problems.append(f"junk + bad checksum: {lines} / S3 {got3!r}")
        finally:
            w.send(";D,STOP")   # the pending ONFIN key is RAM that survives ?DFP,CLEAR
            time.sleep(0.5)
    assert not problems, "; ".join(problems)


@test("dfp.partial_frame_resync", "(should) A truncated DFPlayer frame does not swallow the good finish frame after it", needs=["wcb1"], links=["W2S5", "W2S3"])
def dfp_partial_frame_resync(bench):
    """Probable bug: DfPlayerCodec::poll says a partial frame 'must not swallow the next good one'
    (WcbDfPlayer.cpp:177-178), but it resyncs on 0x7E only while _rxLen == 0 (line 179). A real DFPlayer sends finish
    frames unsolicited, so the ONFIN recall is lost."""
    s5, s3 = link(bench, 2, "S5"), link(bench, 2, "S3")
    _no_recall_keys(bench)
    w = usb_wcb(bench)
    ok, err = f"hilok{nonce().lower()}", f"hilerr{nonce().lower()}"
    with _w2_audio(bench, "DFP", "S5", "S5") as c2, _recall_keys(c2, "DFP", ok, err):
        try:
            prep = _steps(s5, w.send, [(";D,PLAY,5,HILK", _dfp(0x03, 5))])
            time.sleep(0.15)
            s5.send(bytes.fromhex("7E0102"))
            time.sleep(0.1)
            lines, got3 = _inject(c2, s5, s3, DFP_FINISH_5)
        finally:
            w.send(";D,STOP")
            time.sleep(0.5)
    assert not prep, prep
    assert _has(lines, "[DFP] Track 5 finished") and ok.encode() + b"\r" in got3, f"the finish frame was swallowed: {lines} / S3 {got3!r}"


@test("dfp.config_rejects", "?DFP validation on W2 (baud, volume, port, the STATUS parse trap, format, occupied port, host, ONERR length); nothing changes", needs=["wcb1"], links=[])
def dfp_config_rejects(bench):
    if _device_tokens(bench, 2, "DFP"):
        raise Skip("W2 has DFP config")
    host = "[DFP] Invalid host. Use ?DFP,REMOTE,W<n> (1-20, not this board)"
    checks = [
        ("?DFP,S5:38400", ["[DFP] A DFPlayer Mini only runs at 9600 baud (module-fixed)"]),
        ("?DFP,S5:9600:V31", ["[DFP] Volume must be 0-30 (0=silent, 30=loudest)"]),
        ("?DFP,S5:9600:25", ["[DFP] Volume must be given as :V<0-30> (e.g. :V20)"]),
        ("?DFP,S9", ["[DFP] Invalid serial port. Must be S1-S5"]),
        ("?DFP,STATUS", ["[DFP] Invalid serial port. Must be S1-S5"]),     # not a verb: S + 'TATUS'
        ("?DFP,X5", ["[DFP] Invalid format. Use: ?DFP,S<port>[:9600][:V<vol>]", "  Example: ?DFP,S2:9600:V20   (or just ?DFP,S2)"]),
        ("?DFP,REMOTE,W2", [host]), ("?DFP,REMOTE,W21", [host]),
        ("?DFP,ONERR,ABCDEFGHIJKLMNOPQRSTUVWX", ["[DFP] ONERR: key must be 1-15 characters"]),
        ("?DFP,ONERR,ABCDEFGHIJKLMNOP", ["[DFP] ONERR: key must be 1-15 characters"]),   # a sequence key's limit
    ]
    if "?WLED,1:W2S2:115200" in bench.config_tokens(2):
        checks.append(("?DFP,S2", ["[DFP] S2 already in use by PWM/Kyber/HCR/MP3/WLED - config blocked"]))
    _rejects(bench, 2, checks)


# ============================================================ cross-module
def _clear_all_w2(c2):
    """CLEAR on an unconfigured module touches no port, so all three are safe in any state; HCR goes last because
    its release is what leaves the port at 9600 / broadcast ON/ON / no label."""
    for kind in ("DFP", "MP3", "HCR"):
        _run(c2, f"?{kind},CLEAR", 1.0)


@test("conflict.dfp_port_unguarded", "(should) HCR and MP3 refuse a port a DFPlayer already owns, as DFP refuses theirs", needs=["wcb1"], links=["W2S4"])
def dfp_port_unguarded(bench):
    """Probable bug: isSerialPortUsedForDFP is missing from configureHCR (WCB_HCR.cpp:649-652), configureMP3
    (WCB_MP3.cpp:314-317), the WLED guard (WCB_WLED.cpp:326-329) and canUsePWMOnPort (WCB_PWM.cpp:72); only DFP checks
    all the others (WCB_DFP.cpp:267-271). The intruder's CLEAR then re-enables broadcast and deletes the label under
    the still-configured DFPlayer. Everything is recorded and restored before anything is asserted."""
    s4 = link(bench, 2, "S4")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has device config")
    rec = {}
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        try:
            pm = s4.mark()
            c2.expect(r"\[DFP\] Configured: S4 at 9600 baud", since=c2.send("?DFP,S4"))
            c2.expect(r"\[HCR\] Poll interval = 0s", since=c2.send("?HCR,POLL,OFF"))
            for key, cmd in (("hcr", "?HCR,PORT,S4:9600"), ("hcr_clear", "?HCR,CLEAR"), ("mp3", "?MP3,S4:9600:V20"),
                             ("mp3_clear", "?MP3,CLEAR"), ("dfp_clear", "?DFP,CLEAR"),
                             ("poll_off", "?HCR,POLL,OFF"),   # ?HCR,CLEAR put the poll back to 10 s (WCB_HCR.cpp:450)
                             ("hcr_first", "?HCR,PORT,S4:9600"),
                             ("reverse", "?DFP,S4")):
                rec[key] = _run(c2, cmd, 1.0)
            rec["bytes"] = s4.received(pm)
        finally:
            _clear_all_w2(c2)
            _relabel(c2, before[2], "S4")
            _unlearn(bench, "DFP", learner=1)
            _unlearn(bench, "MP3", learner=1)
    bench.note("DFP-port conflict, current behaviour: " + " | ".join(f"{k}: {v}" for k, v in rec.items() if k != "bytes"))
    assert _has(rec["reverse"], "[DFP] S4 already in use by PWM/Kyber/HCR/MP3/WLED - config blocked"), f"DFP took an HCR port: {rec['reverse']}"
    assert not rec["bytes"], f"bytes on W2 S4: {rec['bytes']!r}"
    blocked = lambda lines, kind: any(x.startswith(f"[{kind}] S4 already in use by") and x.endswith("config blocked") for x in lines)
    assert blocked(rec["hcr"], "HCR") and blocked(rec["mp3"], "MP3"), \
        f"HCR accepted the DFPlayer port: {not blocked(rec['hcr'], 'HCR')}; MP3 accepted it: {not blocked(rec['mp3'], 'MP3')}"


@test("backup.roundtrip_all_three", "MP3+DFP+HCR on W2: config tokens and their order; replaying the tokens reproduces the config; clean restore", needs=["wcb1"], links=[])
def roundtrip_all_three(bench):
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    for p in ("S3", "S4", "S5"):
        _require_free(bench, 2, p)
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has device config")
    w = usb_wcb(bench)
    chain = ["?MP3,S3:9600:V64", "?MP3,ONERR,HILE", "?DFP,S5:9600:V0", "?HCR,PORT,S4:9600", "?HCR,POLL,0"]
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        try:
            wm = w.dev.mark()
            for cmd in ("?MP3,S3:9600:V64", "?MP3,ONERR,HILE", "?DFP,S5:9600:V0", "?HCR,POLL,OFF", "?HCR,PORT,S4:9600"):
                _run(c2, cmd, 1.0)
            a = snapshot(bench, 2)
            time.sleep(2)
            learned = w.dev.since(wm)
            _clear_all_w2(c2)
            for cmd in chain:     # the replay: one line each, as a restore would send them
                _run(c2, cmd, 1.0)
            b = snapshot(bench, 2)
        finally:
            _clear_all_w2(c2)
            _relabel(c2, before[2], "S3", "S4", "S5")
            _unlearn(bench, "MP3", learner=1)
            _unlearn(bench, "DFP", learner=1)
    problems = []
    i = a.index(chain[0]) if chain[0] in a else -1
    if i < 0 or a[i:i + len(chain)] != chain:
        problems.append(f"device tokens not consecutive: {[t for t in a if t.startswith(('?MP3', '?DFP', '?HCR'))]}")
    maestro = [k for k, t in enumerate(a) if t.startswith("?MAESTRO")]
    wled = [k for k, t in enumerate(a) if t.startswith("?WLED,")]
    if i >= 0 and ((maestro and maestro[-1] > i) or (wled and wled[0] < i + len(chain))):
        problems.append("device tokens are not between the last ?MAESTRO and the first ?WLED token")
    problems += [f"config lacks {t}" for t in ("?LABEL,S3,MP3 Trigger", "?LABEL,S4,HCR", "?LABEL,S5,DFPlayer") if t not in a]
    problems += [f"config lacks {t}" for p in ("S3", "S4", "S5") for t in (f"?BCAST,OUT,{p},OFF", f"?BCAST,IN,{p},OFF") if t not in a]
    if b != a:
        problems.append(f"replay differs: missing {[t for t in a if t not in b]} / extra {[t for t in b if t not in a]}")
    for name, verb in (("MP3", "A"), ("DFPlayer", "D")):
        if not _has(learned, f"[WDP] {name} host learned — routing ;{verb} to WCB2"):
            problems.append(f"W1 did not learn the {name} host")
    assert not problems, "; ".join(problems)


@test("port.move_release_hcr_mp3", "Re-issuing ?HCR,PORT / ?MP3 on another port releases the old one with that module's own lines; the device follows", needs=["wcb1"], links=["W2S4", "W2S5"])
def move_release_hcr_mp3(bench):
    s4, s5 = link(bench, 2, "S4"), link(bench, 2, "S5")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    for p in ("S4", "S5"):
        _require_free(bench, 2, p)
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has device config")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        mp3_on = False
        try:
            c2.expect(r"\[HCR\] Poll interval = 0s", since=c2.send("?HCR,POLL,OFF"))
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=c2.send("?HCR,PORT,S4:9600"))
            m = c2.send("?HCR,PORT,S5:9600")
            c2.expect(r"\[HCR\] Configured on S5 at 9600 baud", since=m)
            move = c2.lines(m)
            miss = _in_order(move, ["Serial4 label set to: ''", "  ✓ Released S4 (old HCR port)", "  ⚠️  Disabled broadcast output on S5 (HCR port)",
                                    "  ⚠️  Disabled broadcast input on S5 (HCR port)", "Serial5 label set to: 'HCR'",
                                    "[HCR] Active on S5 at 9600 baud, poll=0s", "[HCR] Configured on S5 at 9600 baud"])
            if miss or _has(move, "Re-enabled"):
                problems.append(f"HCR move printed {move}")
            watch = Watch(s4, s5)
            w.send(";H,OVERLOAD")
            try:
                watch.expect(s5, _lf("<SE,QT>"), timeout=3)
            except AssertionError:
                problems.append("OVERLOAD did not follow HCR to S5")
            time.sleep(0.5)
            if watch.got(s4):
                problems.append(f"bytes on the old HCR port: {watch.got(s4)!r}")
            tokens = snapshot(bench, 2)
            problems += [f"config lacks {t}" for t in ("?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON", "?BCAST,OUT,S5,OFF", "?BCAST,IN,S5,OFF",
                                                       "?LABEL,S5,HCR", "?HCR,PORT,S5:9600") if t not in tokens]
            if [t for t in tokens if t.startswith("?LABEL,S4,")]:
                problems.append("S4 kept a label after the move")
            m = c2.send("?HCR,CLEAR")
            c2.expect(r"  ✓ Released S5 \(old HCR port\)", since=m)
            miss = _in_order(c2.lines(m), [x.replace("S4", "S5").replace("Serial4", "Serial5") for x in HCR_RELEASE_S4])
            if miss:
                problems.append(f"HCR CLEAR after the move lacks {miss!r}")
            wm = w.dev.mark()
            m = c2.send("?MP3,S4:9600:V20")
            mp3_on = True
            c2.expect(r"\[MP3\] Configured: S4 at 9600 baud", since=m)
            try:
                w.dev.expect(r"\[WDP\] MP3 host learned — routing ;A to WCB2", timeout=5, since=wm)
            except AssertionError:
                problems.append("W1 did not learn the MP3 host")
            m = c2.send("?MP3,S5:9600:V20")
            c2.expect(r"\[MP3\] Configured: S5 at 9600 baud", since=m)
            miss = _in_order(c2.lines(m), ["  ✓ Re-enabled broadcast output on S4 (old MP3 port)", "  ✓ Re-enabled broadcast input on S4 (old MP3 port)",
                                           "Serial4 label set to: ''", "  ✓ Released S4 (old MP3 port)",
                                           "  ⚠️  Disabled broadcast output on S5 (MP3 Trigger port)", "  ⚠️  Disabled broadcast input on S5 (MP3 Trigger port)",
                                           "Serial5 label set to: 'MP3 Trigger'", "[MP3] Configured: S5 at 9600 baud  default volume=20"])
            if miss:
                problems.append(f"MP3 move lacks {miss!r}")
            watch = Watch(s4, s5)
            w.send(";A,PLAY,3")
            try:
                watch.expect(s5, bytes.fromhex("76147403"), timeout=3)
            except AssertionError:
                problems.append("PLAY did not follow the MP3 Trigger to S5")
            time.sleep(0.5)
            if watch.got(s4):
                problems.append(f"bytes on the old MP3 port: {watch.got(s4)!r}")
        finally:
            _clear_all_w2(c2)
            _relabel(c2, before[2], "S4", "S5")
            if mp3_on:
                _unlearn(bench, "MP3", learner=1)
    assert not problems, "; ".join(problems)


@test("devices.soft_ports_fast_baud", "An MP3 Trigger at 38400 on W2 S3 and an HCR at 57600 on W2 S4 are accepted (soft ports take up to 57600 since 2026-09-24, F1): the config lines, the chain, and each device's exact bytes at that rate; cleared and restored after", needs=["wcb1"], links=["W2S3", "W2S4"])
def soft_ports_fast_baud(bench):
    """The HCR / MP3 / WLED guards refused anything above 9600 on S3-S5, a limit from the bit-banged days, while ?BAUD
    allowed 57600 (received exactly through 57600, 0/20 lines at 115200: CLAUDE.md rule 13). Since 2026-09-24 all of
    them agree at 57600 (docs/HIL_TEST_AUDIT.md F1, tracker #81); 115200 stays refused (hcr.config_rejects,
    wled.config_rejects). Same flow as boot.devices_restore_w2: configure on W2's own console, prove the bytes from W1
    through its routes, clear, relabel, and unlearn the MP3 route W1 persisted."""
    s3, s4 = link(bench, 2, "S3"), link(bench, 2, "S4")
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    for p in ("S3", "S4"):
        _require_free(bench, 2, p)
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has device config")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        try:
            out = _run(c2, "?MP3,S3:38400:V10", 1.0)
            miss = _in_order(out, ["Baud rate for Serial3 updated to 38400", "  ⚠️  Disabled broadcast output on S3 (MP3 Trigger port)",
                                   "  ⚠️  Disabled broadcast input on S3 (MP3 Trigger port)", "Serial3 label set to: 'MP3 Trigger'",
                                   "[MP3] Configured: S3 at 38400 baud  default volume=10"])
            if miss:
                problems.append(f"?MP3,S3:38400:V10 output lacks {miss!r} (in order)")
            if _has(out, "SOFTWARE SERIAL") or _has(out, "BLOCKED"):
                problems.append("?MP3,S3:38400 was refused as software serial")
            _run(c2, "?HCR,POLL,OFF", 1.0)
            out = _run(c2, "?HCR,PORT,S4:57600", 1.0)
            miss = _in_order(out, ["Baud rate for Serial4 updated to 57600", "  ⚠️  Disabled broadcast output on S4 (HCR port)",
                                   "  ⚠️  Disabled broadcast input on S4 (HCR port)", "[HCR] Configured on S4 at 57600 baud"])
            if miss:
                problems.append(f"?HCR,PORT,S4:57600 output lacks {miss!r} (in order)")
            if _has(out, "SOFTWARE SERIAL") or _has(out, "BLOCKED"):
                problems.append("?HCR,PORT,S4:57600 was refused as software serial")
            time.sleep(2)
            toks = snapshot(bench, 2)
            problems += [f"W2's chain lacks {t}" for t in ("?MP3,S3:38400:V10", "?BAUD,S3,38400", "?HCR,PORT,S4:57600", "?BAUD,S4,57600")
                         if t not in toks]
            s3.listen(38400)
            s4.listen(57600)
            problems += _steps(s3, w.send, [(";A,PLAY,7", bytes.fromhex("760A7407"))])
            problems += _steps(s4, w.send, [(";H,OVERLOAD", _lf("<SE,QT>"))])
        finally:
            _clear_all_w2(c2)
            _relabel(c2, before[2], "S3", "S4")
            _unlearn(bench, "MP3", learner=1)
    assert not problems, "; ".join(problems)


@test("devices.maestro_port_refused", "A local Maestro's port refuses a WLED, an MP3 Trigger, a DFPlayer and an HCR ('S2 is a local Maestro's port - config blocked'), and a port a WLED holds refuses a local Maestro; each refusal changes nothing (re-scan #15; W1's WDP off)", needs=["wcb1"], links=[])
def maestro_port_refused(bench):
    """WCB coverage re-scan #15 (docs/hil_plan/WCB.md WCB-WP26 row 3). The HCR, MP3, DFPlayer and WLED guards checked
    each other, PWM and the Kyber but never a local Maestro, and a local ?MAESTRO add checked only the Kyber port: a
    Maestro and a device could share a UART, and one module's CLEAR reset the port's baud and flags under the other.
    The existing '... already in use by ...' lines are matched by tests and tools, so the Maestro conflict has its own
    line. W1's WDP is off throughout, so no board or client learns the throwaway Maestro 5 or WLED 5 (s22's rules)."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            undo = []
            try:
                out = w.run("?MAESTRO,M5:W1S2:9600")
                if not _has(out, "Maestro 5: Local S2 at 9600 baud"):
                    raise AssertionError(f"setup: ?MAESTRO,M5:W1S2:9600 printed {out}")
                undo.append("?MAESTRO,CLEAR,M5:W1S2")
                for cmd, kind in (("?WLED,5:W1S2:9600", "WLED"), ("?MP3,S2:9600:V20", "MP3"), ("?DFP,S2", "DFP"),
                                  ("?HCR,PORT,S2:9600", "HCR")):
                    out = [x.rstrip() for x in w.run(cmd)]
                    if f"[{kind}] S2 is a local Maestro's port - config blocked" not in out:
                        problems.append(f"{cmd} beside Maestro 5 printed {out}")
                w.run(undo.pop())
                out = w.run("?WLED,5:W1S2:9600")
                if not _has(out, "[WLED] WLED 5: local S2 at 9600 baud"):
                    raise AssertionError(f"setup: ?WLED,5:W1S2:9600 printed {out}")
                undo.append("?WLED,CLEAR,5")
                out = [x.rstrip() for x in w.run("?MAESTRO,M5:W1S2:9600")]
                if "❌ Maestro 5: S2 is the WLED port - use another port, or clear it first." not in out:
                    problems.append(f"?MAESTRO,M5:W1S2:9600 beside WLED 5 printed {out}")
            finally:
                for cmd in reversed(undo):
                    w.run(cmd)
                # A WLED labels its port 'WLED <id>' and its CLEAR empties the label (s24 pins both), so S2's own
                # label goes back by hand.
                w.run(token(before[1], "?LABEL,S2,") or "?LABEL,CLEAR,S2")
    assert not problems, "; ".join(problems)


# ============================================================ capability routing (WCB-WP26 rows 1-2)
CAP_HCR, CAP_MP3, CAP_DFP = 0x0001, 0x0002, 0x0100   # WDP_CAP_* (WCB_WDP.h:44-52)
# The old MgmtRelay's id. No bench device or test ever transmits as it (common.FORBIDDEN_MESH_IDS), so W1 has never
# heard it since its boot - unlike 3-18, which the s05/s18/s19/s21/s22 probe clients all take at some point of a run.
NEVER_HEARD = 19
PIN_ID = 7              # the probe's client id in route.pin_dead_fails_over; it only listens (the MESH_IDS rule, s19)


def _wdp_row(w, n):
    return next((x for x in w.run("?WDP,DUMP", timeout=8) if x.startswith(f"[WDP:N={n},")), None)


def _cap_bits(w, n):
    """W<n>'s capability bitmap in W1's WDP table, or None when W1 has no row for it."""
    m = re.search(r"CAP=([0-9A-Fa-f]+)", _wdp_row(w, n) or "")
    return int(m.group(1), 16) if m else None


def _await_cap(w, n, bit, present, timeout=12.0):
    """Wait until W1's WDP row for W<n> does (present) or does not advertise `bit`, with ?WDP,POLL between reads (W<n>
    answers the solicit with an advert). -> whether it got there."""
    deadline = time.monotonic() + timeout
    while True:
        cap = _cap_bits(w, n)
        if cap is not None and bool(cap & bit) == present:
            return True
        if time.monotonic() >= deadline:
            return False
        w.run("?WDP,POLL")
        time.sleep(1.5)


def _routes(lines):
    return [x.rstrip() for x in lines if x.startswith("[ROUTE]")]


def _in_order_sub(lines, wanted):
    """Like _in_order, but each wanted text only has to be IN its line, so the ✓ / ⚠️ prefixes need not match byte for
    byte; returns the first one missing."""
    i = 0
    for x in lines:
        if i < len(wanted) and wanted[i] in x:
            i += 1
    return None if i == len(wanted) else wanted[i]


def _wait_mesh_rx(probe, since, sender, text, timeout=3.0):
    """The first command the probe (a mesh client) received from W<sender> starting with `text`, or None."""
    deadline = time.monotonic() + timeout
    while True:
        hit = next((x for s, x in probe.mesh_received(since) if s == sender and x.startswith(text)), None)
        if hit is not None or time.monotonic() >= deadline:
            return hit
        time.sleep(0.1)


@test("route.pin_never_heard_honoured", "A capability pinned to a board W1 has not heard since its boot (WCB19) is honoured: ;H,MUSE goes there, not to W2, the live HCR owner", needs=["wcb1"], links=["W2S4"])
def pin_never_heard_honoured(bench):
    """WCB-WP26 row 1, case (a). routeStoredOrCap (WCB.ino:7725-7743) fails over only when there is no pin, or the pin
    is 'genuinely dead': heard this session (wcbPeerEverSeen, :9004-9007; lastSeenMs is never reset) and offline now
    (wcbPeerOnline, :8985-8997). A pin not heard since boot is honoured even while another board advertises the
    capability, so an operator's pin survives the boot window. The plan's id 7 does not work for this case in a full
    run: probe clients take 7 earlier (s05 CLIENT_IDS, s19 MESH_IDS), and a board W1 has heard once is dead, not
    unheard. WCB19 is never used (NEVER_HEARD)."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    if _wdp_row(w, NEVER_HEARD):
        raise Skip(f"WCB{NEVER_HEARD} is in W1's WDP table, so W1 has heard it")
    if any(re.match(rf"^  WCB{NEVER_HEARD}: ", x) for x in w.config_lines()):
        raise Skip(f"WCB{NEVER_HEARD} is one of W1's peers")
    problems = []
    with _hcr_w2(bench):
        try:
            if not _await_cap(w, 2, CAP_HCR, True):
                raise AssertionError("setup: W1 never saw W2 advertise its HCR, so no live owner competes with the pin")
            out = [x.rstrip() for x in w.run(f"?HCR,REMOTE,W{NEVER_HEARD}")]
            if not _has(out, f"[HCR] Routing ;H to WCB{NEVER_HEARD}"):
                raise AssertionError(f"setup: ?HCR,REMOTE,W{NEVER_HEARD} printed {out}")
            w.run("?DEBUG,ETM,ON")
            sm, wm = s4.mark(), w.dev.mark()
            w.send(";H,MUSE")
            time.sleep(1.5)
            routes = _routes(w.dev.since(wm))
            if routes != [f"[ROUTE] ;H,MUSE -> host WCB{NEVER_HEARD}"]:
                problems.append(f"W1 routed {routes} (WCB2 would mean the unheard pin was taken for a dead one)")
            if s4.received(sm):
                problems.append(f"W2, the live owner, got it anyway: {s4.received(sm)!r}")
            if not _has(w.run("?HCR,LIST"), f"  Routes ;H to WCB{NEVER_HEARD} (remote host)"):
                problems.append("?HCR,LIST does not show the pin")
        finally:
            w.run("?DEBUG,ETM,OFF")
            w.run("?HCR,REMOTE,W2")
    assert not problems, "; ".join(problems)


@test("route.pin_dead_fails_over", "A pin W1 has heard and then lost fails over to W2, the live HCR owner, and the stored route is not rewritten; with no live owner left the dead pin stays the target; while the pinned client is alive it gets the command (probe as client 7, ~50 s)", needs=["wcb1", "probe1"], links=["W2S4"])
def pin_dead_fails_over(bench):
    """WCB-WP26 row 1, cases (b) and (c), plus the live pin. routeStoredOrCap (WCB.ino:7725-7743): a pin that is heard
    and online is honoured; once it has been heard and is offline (pinGenuinelyDead, :7733) wdpCapOwner (WCB_WDP.cpp:
    458-468) picks the lowest live owner, and with none the stored host stays the best-effort target (:7734-7738).
    Neither case writes hcrConfig.remoteWCB. The pin is the probe as a temporary client: W1 adopts it (addTemporaryPeer
    stamps lastSeenMs, WCB.ino:9086-9091) and marks it online on its heartbeat. The plan waits out the ~50 s eviction;
    probe_in_mesh's ?WDP,FORGET does the same at once (removeActivePeer -> boardMarkOffline, :9101-9129, which keeps
    lastSeenMs), so the pin is dead as soon as the probe has left. W1's route is pinned by hand before, so W2's HCR
    advert changes nothing (hcrAutoAddRemote is first-host-wins, WCB_HCR.cpp:1021-1029)."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip(f"W2 already has HCR/MP3/DFP config: {_device_tokens(bench, 2)}")
    if "?WDP,AUTOJOIN,OFF" in bench.config_tokens(1):
        raise Skip("W1's auto-join is off, so it would never adopt the probe")
    if _wdp_row(w, PIN_ID):
        raise Skip(f"mesh id {PIN_ID} is already in W1's WDP table")
    problems = []
    alive = {}
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        hosted = False
        try:
            c2.expect(r"\[HCR\] Poll interval = 0s", since=c2.send("?HCR,POLL,OFF"))
            m = c2.send("?HCR,PORT,S4:9600")
            hosted = True
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=m)
            if not _await_cap(w, 2, CAP_HCR, True):
                raise AssertionError("setup: W1 never saw W2 advertise its HCR")
            out = [x.rstrip() for x in w.run(f"?HCR,REMOTE,W{PIN_ID}")]
            if not _has(out, f"[HCR] Routing ;H to WCB{PIN_ID}"):
                raise AssertionError(f"setup: ?HCR,REMOTE,W{PIN_ID} printed {out}")
            w.run("?DEBUG,ETM,ON")
            wm = w.dev.mark()
            with probe_in_mesh(bench, "probe1", PIN_ID) as probe:
                w.dev.expect(rf"\[WDP\] temporarily joined WCB{PIN_ID} ", timeout=15, since=wm)
                w.dev.expect(rf"\[ETM\] WCB{PIN_ID} came ONLINE", timeout=25, since=wm)
                pm, sm, rm = probe.dev.mark(), s4.mark(), w.dev.mark()
                w.send(";H,MUSE")
                alive["rx"] = _wait_mesh_rx(probe, pm, bench.usb_wcb_number(), ";H,MUSE", timeout=3.0)
                time.sleep(0.5)
                alive["routes"], alive["s4"] = _routes(w.dev.since(rm)), s4.received(sm)
            # Left and forgotten on every WCB: W1 has marked the client offline and keeps its lastSeenMs.
            sm, rm = s4.mark(), w.dev.mark()
            w.send(";H,MUSE")
            try:
                s4.expect(_lf("<MM>"), timeout=3, since=sm)
            except AssertionError:
                problems.append(f"the dead pin did not fail over to W2: W2 S4 got {s4.received(sm)!r}")
            time.sleep(0.3)
            routes = _routes(w.dev.since(rm))
            if routes != ["[ROUTE] ;H,MUSE -> host WCB2"]:
                problems.append(f"dead pin, live owner: W1 routed {routes}")
            if f"?HCR,REMOTE,W{PIN_ID}" not in snapshot(bench, 1):
                problems.append("the failover rewrote W1's stored route")
            m = c2.send("?HCR,CLEAR")
            c2.expect(r"  ✓ Released S4 \(old HCR port\)", since=m)
            hosted = False
            _relabel(c2, before[2], "S4")
            if not _await_cap(w, 2, CAP_HCR, False):
                problems.append("W1 still sees W2 advertise an HCR after its CLEAR")
            sm, rm = s4.mark(), w.dev.mark()
            w.send(";H,MUSE")
            time.sleep(1.5)
            routes = _routes(w.dev.since(rm))
            if routes != [f"[ROUTE] ;H,MUSE -> host WCB{PIN_ID}"]:
                problems.append(f"dead pin, no live owner: W1 routed {routes} (the pin should stay the target)")
            if s4.received(sm):
                problems.append(f"bytes on W2 S4 with no HCR there: {s4.received(sm)!r}")
        finally:
            w.run("?DEBUG,ETM,OFF")
            if hosted:
                m = c2.send("?HCR,CLEAR")
                c2.expect(r"  ✓ Released S4 \(old HCR port\)", since=m)
                _relabel(c2, before[2], "S4")
            w.run("?HCR,REMOTE,W2")
    if alive.get("rx") is None:
        problems.append(f"the pinned client, alive, never received ;H,MUSE (W1 routed {alive.get('routes')})")
    if alive and alive.get("routes") != [f"[ROUTE] ;H,MUSE -> host WCB{PIN_ID}"]:
        problems.append(f"live pin: W1 routed {alive.get('routes')}")
    if alive.get("s4"):
        problems.append(f"live pin: W2 S4 got {alive['s4']!r}")
    assert not problems, "; ".join(problems)


@test("route.offline_owner_skipped", "With no pin and auto-join off, ;H,MUSE goes to W2, the live HCR owner; once W1 has marked W2 offline (W2's ETM off for about a minute) it runs locally ('Not configured') instead of going to W2", needs=["wcb1", "wcb2"], links=["W2S4"])
def offline_owner_skipped(bench):
    """WCB-WP26 row 1, case (d). wdpCapOwner (WCB_WDP.cpp:458-468) skips an owner that is not wcbPeerOnline, because
    WDP's 180 s TTL outlives the ETM offline window: W1's WDP row still says W2 hosts the HCR, and routing there would
    lose the trigger. With no owner left and no pin, routeStoredOrCap runs the command locally (WCB.ino:7734-7738).
    The plan deafens W2 with a ?MAC,3 flip; this week's bench rules forbid touching MAC octets, so W2's ETM goes off on
    its own USB console instead: it then sends no heartbeat (processETMHeartbeats, WCB.ino:1361-1362) and W1 drops its
    non-ETM frames before they refresh presence (:5194-5221, :5277-5302), so W1 marks W2 offline after
    (HB+1)*MISS seconds (:1382). W1's auto-join is off and its route cleared, as in route.live_election_autojoin_off."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?HCR,REMOTE,W2", "?ETM,ON")
    require_tokens(bench, 2, "?ETM,ON")
    if "?WDP,AUTOJOIN,OFF" in bench.config_tokens(1):
        raise Skip("W1 already has auto-join off")
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip(f"W2 already has HCR/MP3/DFP config: {_device_tokens(bench, 2)}")

    def etm_value(wcb, key, default):
        t = token(bench.config_tokens(wcb), f"?ETM,{key},")
        return int(t.split(",")[2]) if t and t.split(",")[2].isdigit() else default

    window = (etm_value(1, "HB", 10) + 1) * etm_value(1, "MISS", 5)
    if window > 120:
        raise Skip(f"W1's offline window is {window} s")
    hb2 = etm_value(2, "HB", 10)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2's ETM goes off, so its console must be its own USB port")
        hosted = etm_off = False
        try:
            if not _has(w.run("?WDP,AUTOJOIN,OFF"), "[WDP] auto-join disabled"):
                raise AssertionError("setup: ?WDP,AUTOJOIN,OFF did not confirm")
            if not _has(w.run("?HCR,REMOTE,OFF"), "[HCR] Remote routing cleared"):
                raise AssertionError("setup: ?HCR,REMOTE,OFF did not confirm")
            c2.expect(r"\[HCR\] Poll interval = 0s", since=c2.send("?HCR,POLL,OFF"))
            m = c2.send("?HCR,PORT,S4:9600")
            hosted = True
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=m)
            if not _await_cap(w, 2, CAP_HCR, True):
                raise AssertionError("setup: W1 never saw W2 advertise its HCR")
            w.run("?DEBUG,ETM,ON")
            sm, rm = s4.mark(), w.dev.mark()
            w.send(";H,MUSE")
            try:
                s4.expect(_lf("<MM>"), timeout=3, since=sm)
            except AssertionError:
                problems.append("control: with W2 online the live election did not reach W2 S4")
            time.sleep(0.3)
            if _routes(w.dev.since(rm)) != ["[ROUTE] ;H,MUSE -> host WCB2"]:
                problems.append(f"control: W1 routed {_routes(w.dev.since(rm))}")
            w.run("?DEBUG,ETM,OFF")
            wm = w.dev.mark()
            out = _run(c2, "?ETM,OFF")
            if not _has(out, "ETM disabled"):
                raise AssertionError(f"setup: W2's ?ETM,OFF printed {out}")
            etm_off = True
            w.dev.expect(r"\[ETM\] WCB2 went OFFLINE", timeout=window + 15, since=wm)
            cap = _cap_bits(w, 2)
            if cap is None or not cap & CAP_HCR:
                raise AssertionError(f"setup: W1's WDP row for W2 no longer holds the HCR (CAP={cap}), so the gate is not "
                                     "what would send ;H local")
            w.run("?DEBUG,ETM,ON")
            wm = w.dev.mark()
            w.send(";H,MUSE")
            time.sleep(1.5)
            lines = [x.rstrip() for x in w.dev.since(wm)]
            if not _has(lines, "[HCR] Not configured"):
                problems.append("with W2 offline, ;H,MUSE did not run locally on W1 ('[HCR] Not configured')")
            if _routes(lines):
                problems.append(f"W1 still routed to the offline owner: {_routes(lines)}")
        finally:
            w.run("?DEBUG,ETM,OFF")
            if etm_off:
                wm = w.dev.mark()
                if not _has(_run(c2, "?ETM,ON"), "ETM enabled"):
                    problems.append("W2's ?ETM,ON did not confirm")
                try:
                    w.dev.expect(r"\[ETM\] WCB2 came ONLINE", timeout=hb2 + 12, since=wm)
                except AssertionError:
                    problems.append("W2 did not come back online on W1 after ?ETM,ON")
            if hosted:
                m = c2.send("?HCR,CLEAR")
                c2.expect(r"  ✓ Released S4 \(old HCR port\)", since=m)
                _relabel(c2, before[2], "S4")
            w.run("?HCR,REMOTE,W2")      # before auto-join returns, so no advert can race it
            w.run("?WDP,AUTOJOIN,ON")
    assert not problems, "; ".join(problems)


@test("route.controller_pin", "A capability pinned to the controller (?HCR,REMOTE,W20) is honoured while NaviCore heartbeats: ;H,MUSE goes to WCB20, which ACKs and logs it, not to W2, the live HCR owner", needs=["wcb1", "navicore"], links=["W2S4"])
def controller_pin(bench):
    """WCB-WP26 row 2. The controller is never in wcbPeerActive[] (addActivePeer refuses it), so before wcbPeerOnline
    counted the special peer (WCB.ino:8985-8997) a live controller read as unreachable, a pin to it was 'genuinely
    dead' on every trigger (:7733) and was re-elected to the other owner. The ETM unicast to it expects its ACK
    (etmAddToPendingTable's special-peer slot, :1527-1545). NaviCore does not act on ;H: onWCBCommand logs it as an
    unhandled command under DBG_MAESTRO (NaviCore.ino, '[WCB RX] from WCB<n>: ...'); its one lone debug line is
    released with #L12 (docs/HIL_TESTING.md §5)."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    require_tokens(bench, 1, "?CONTROLLER,ON,20")
    if not w.etm_board_stats().get(20, {}).get("online"):
        raise Skip("NaviCore (WCB20) is not online on W1")
    nc = NaviCore(bench.dev("navicore"))
    problems = []
    with _hcr_w2(bench):
        try:
            if not _await_cap(w, 2, CAP_HCR, True):
                raise AssertionError("setup: W1 never saw W2 advertise its HCR, so no live owner competes with the pin")
            out = [x.rstrip() for x in w.run("?HCR,REMOTE,W20")]
            if not _has(out, "[HCR] Routing ;H to WCB20"):
                raise AssertionError(f"setup: ?HCR,REMOTE,W20 printed {out}")
            w.run("?DEBUG,ETM,ON")
            with nc.debug(DBG_MAESTRO):
                nm, sm, wm = nc.dev.mark(), s4.mark(), w.dev.mark()
                w.send(";H,MUSE")
                time.sleep(2.0)
                nc.mode()
                ncl = [x.rstrip() for x in nc.dev.since(nm)]
            lines = [x.rstrip() for x in w.dev.since(wm)]
            if _routes(lines) != ["[ROUTE] ;H,MUSE -> host WCB20"]:
                problems.append(f"W1 routed {_routes(lines)}")
            seq = next((m.group(1) for m in (re.search(r"\[ETM\] Sent seq (\d+): ;H,MUSE$", x) for x in lines) if m), None)
            if seq is None or f"[ETM] ACK received from WCB20 for seq {seq}" not in lines:
                problems.append(f"no ACK from WCB20 for the ;H,MUSE unicast (seq {seq})")
            if f"[WCB RX] from WCB{me}: ;H,MUSE" not in ncl:
                problems.append("NaviCore did not log the inbound ;H,MUSE")
            if s4.received(sm):
                problems.append(f"W2, the other owner, got it anyway: {s4.received(sm)!r}")
        finally:
            w.run("?DEBUG,ETM,OFF")
            w.run("?HCR,REMOTE,W2")
    assert not problems, "; ".join(problems)


# ============================================================ port ownership (WCB-WP26 row 3)
BLOCKED = {"HCR": "[HCR] S{p} already in use by PWM/Kyber/MP3/WLED/DFP - config blocked",
           "MP3": "[MP3] S{p} already in use by PWM/Kyber/HCR/WLED/DFP - config blocked",
           "DFP": "[DFP] S{p} already in use by PWM/Kyber/HCR/MP3/WLED - config blocked",
           "WLED": "[WLED] S{p} already in use by PWM/Kyber/MP3/HCR/DFP - config blocked"}
TOOK = {"HCR": "[HCR] Configured on S", "MP3": "[MP3] Configured: S", "DFP": "[DFP] Configured: S",
        "WLED": "[WLED] WLED 6: local S"}


def _claim(kind, port, wcb):
    """The command that configures `kind` on W<wcb> <port> (WLED id 6)."""
    return {"HCR": f"?HCR,PORT,{port}:9600", "MP3": f"?MP3,{port}:9600:V20", "DFP": f"?DFP,{port}",
            "WLED": f"?WLED,6:W{wcb}{port}:9600"}[kind]


@contextmanager
def _w2_wdp_off(c2, tokens):
    """W2's WDP off for the block (its own console): a device it takes is not advertised, so W1 learns no route."""
    if "?WDP,OFF" in tokens:
        raise Skip("W2's WDP is already off, so this test could not tell its own ?WDP,OFF apart from the bench's")
    if not _has(_run(c2, "?WDP,OFF"), "[WDP] disabled"):
        raise AssertionError("W2's ?WDP,OFF did not confirm")
    try:
        yield
    finally:
        _run(c2, "?WDP,ON")


def _config_diff(before, after):
    return f"missing {[t for t in before if t not in after]} / extra {[t for t in after if t not in before]}"


@test("devices.port_owner_refusals", "On W2 S4 an HCR refuses an MP3 Trigger, a DFPlayer and a WLED; an MP3 Trigger refuses an HCR, a DFPlayer and a WLED; a DFPlayer refuses an HCR, an MP3 Trigger and a WLED; each refusal prints its 'already in use' line and changes nothing (W2's WDP off)", needs=["wcb1"], links=[])
def port_owner_refusals(bench):
    """WCB-WP26 row 3, the device pairs. Each configure path refuses a port another device owns before it touches the
    port: configureHCR (WCB_HCR.cpp:887-892), configureMP3 (WCB_MP3.cpp:308-313), configureDFP (WCB_DFP.cpp:269-274),
    configureWLED (WCB_WLED.cpp:333-338). Covered elsewhere: a WLED port refusing the other three
    (hcr/mp3/dfp.config_rejects on W2 S2), the first half of the DFPlayer pairs (conflict.dfp_port_unguarded) and a
    local Maestro's port both ways (devices.maestro_port_refused, the plan's BUG-15, fixed). W2's WDP is off, so a
    device the firmware took by mistake is never advertised and W1 learns no route."""
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip(f"W2 already has HCR/MP3/DFP config: {_device_tokens(bench, 2)}")
    if any(t.upper().startswith("?WLED,6:") for t in bench.config_tokens(2)):
        raise Skip("W2 already has a WLED 6")
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2, _w2_wdp_off(c2, before[2]):
        try:
            _run(c2, "?HCR,POLL,OFF")
            for owner in ("HCR", "MP3", "DFP"):
                out = _run(c2, _claim(owner, "S4", 2), 1.0)
                if not _has(out, TOOK[owner] + "4"):
                    raise AssertionError(f"setup: {_claim(owner, 'S4', 2)} printed {out}")
                base = snapshot(bench, 2)
                for kind in ("HCR", "MP3", "DFP", "WLED"):
                    if kind == owner:
                        continue
                    cmd = _claim(kind, "S4", 2)
                    out = _run(c2, cmd, 1.0)
                    if not _has(out, BLOCKED[kind].format(p=4)):
                        problems.append(f"{owner} on S4: {cmd} printed {out}")
                after = snapshot(bench, 2)
                if after != base:
                    problems.append(f"{owner} on S4: the refusals changed W2's config: {_config_diff(base, after)}")
                _run(c2, f"?{owner},CLEAR", 1.0)
        finally:
            _clear_all_w2(c2)
            _run(c2, "?WLED,CLEAR,6")        # 'not configured' unless a refusal failed
            _relabel(c2, before[2], "S4")
    assert not problems, "; ".join(problems)


@test("devices.pwm_port_refused", "On W1 a PWM input (S3), a PWM mapping's output (S4) and an output-only PWM port (S5) each refuse an HCR, an MP3 Trigger, a DFPlayer and a WLED ('already in use by PWM/...'), and nothing changes (W1's WDP off; 2 reboots)", needs=["wcb1"],
      drives=["W1S4", "W1S5"])   # the mapping's output S4 and the output-only S5 are PWM outputs until the clear
def pwm_port_refused(bench):
    """WCB-WP26 row 3, a device against PWM. Every device guard starts with isSerialPortPWMOutput ||
    isSerialPortUsedForPWMInput (WCB_HCR.cpp:887, WCB_MP3.cpp:308, WCB_DFP.cpp:269, WCB_WLED.cpp:333). The output
    check has two branches, the output-only list and a mapping's local outputs (WCB_PWM.cpp:937-960), so one mapping
    S3 -> S4 plus ?MAP,PWM,OUT,S5 covers all three owners: one reboot applies them, ?MAP,PWM,CLEAR,ALL's reboot undoes
    both (clearAllPWMMappings, WCB_PWM.cpp:607-684). On W1, not W2 as the plan has it: the guards are the same code, and
    W1's WDP is off, so a device the firmware took by mistake is never advertised. The PWM input is held LOW by the
    probe (s14: never leave one floating)."""
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _no_pwm(bench, 1)
    for p in ("S3", "S4", "S5"):
        _require_free(bench, 1, p)
    if any(t.upper().startswith("?WLED,6:") for t in bench.config_tokens(1)):
        raise Skip("W1 already has a WLED 6")
    problems, taken = [], []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            pwm = False
            try:
                s3.pwm_out(0)
                out = w.run("?MAP,PWM,OUT,S5")
                pwm = True
                if not _has(out, "Serial5 configured as PWM output port"):
                    raise AssertionError(f"setup: ?MAP,PWM,OUT,S5 printed {out}")
                m = w.send("?MAP,PWM,S3,S4")
                _pwm_reboot(w, m)
                base = snapshot(bench, 1)
                pwm_toks = [t for t in base if t.upper().startswith("?MAP,PWM")]
                if "?MAP,PWM,OUT,S5" not in base or not any(t.upper().startswith("?MAP,PWM,S3") for t in base):
                    raise AssertionError(f"setup: W1's chain holds {pwm_toks}")
                for port in ("S3", "S4", "S5"):
                    for kind in ("HCR", "MP3", "DFP", "WLED"):
                        cmd = _claim(kind, port, me)
                        out = [x.rstrip() for x in w.run(cmd)]
                        if _has(out, TOOK[kind]):
                            taken.append(kind)
                        if not _has(out, BLOCKED[kind].format(p=port[1])):
                            problems.append(f"{cmd} on a PWM port printed {out}")
                after = snapshot(bench, 1)
                if after != base:
                    problems.append(f"the refusals changed W1's config: {_config_diff(base, after)}")
            finally:
                for kind in dict.fromkeys(taken):          # taken by mistake: undo it while W1's WDP is still off
                    w.run("?WLED,CLEAR,6" if kind == "WLED" else f"?{kind},CLEAR")
                if "HCR" in taken and "?HCR,REMOTE,W2" in before[1]:
                    w.run("?HCR,REMOTE,W2")                 # a local HCR zeroes the route (WCB_HCR.cpp:736)
                if taken:
                    for p in ("S3", "S4", "S5"):
                        w.run(token(before[1], f"?LABEL,{p},") or f"?LABEL,CLEAR,{p}")
                if pwm:
                    m = w.send("?MAP,PWM,CLEAR,ALL")
                    try:
                        _pwm_reboot(w, m)
                    except AssertionError as e:
                        problems.append(f"?MAP,PWM,CLEAR,ALL did not reboot W1: {e}")
                s3.pwm_stop()
    assert not problems, "; ".join(problems)


@test("devices.serial_mapped_port_refused", "(should) A port a serial mapping reads refuses an HCR, an MP3 Trigger and a DFPlayer, and a serial mapping refuses an HCR's port as its input, as both sides refuse PWM: the device owns the port's RX, so the mapping would read nothing (W2's WDP off)", needs=["wcb1"], links=[])
def serial_mapped_port_refused(bench):
    """Found while writing WCB-WP26 row 3; not a plan row. processIncomingSerial returns before reading an MP3,
    DFPlayer or HCR port (WCB.ino:8404-8412), so a mapping whose input is such a port never sees a byte, and neither side
    checks the other: configureHCR / configureMP3 / configureDFP have no serial-mapping term (WCB_HCR.cpp:887-899,
    WCB_MP3.cpp:308-320, WCB_DFP.cpp:269-281), and addSerialMonitorMapping refuses only PWM ports as its input
    (WCB_Storage.cpp:2119-2127). PWM got the rule both ways on 2026-09-27 (serialMapOwnsPort, WCB_PWM.cpp;
    docs/HIL_WEEK_DECISIONS.md D23), and the same shared port-owner check is what re-scan #15 recommends. A WLED is left
    out: it only transmits, so a mapping can still read its port. Asserted on behaviour, not wording: whatever the
    firmware takes is cleared at once, before the next is tried."""
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip(f"W2 already has HCR/MP3/DFP config: {_device_tokens(bench, 2)}")
    if any(t.upper().startswith("?MAP,SERIAL,S4") for t in bench.config_tokens(2)):
        raise Skip("W2 already maps S4")
    took = {}
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2, _w2_wdp_off(c2, before[2]):
        mapped = False
        try:
            _run(c2, "?HCR,POLL,OFF")
            out = _run(c2, "?MAP,SERIAL,S4,S0", 1.0)
            if not _has(out, "Serial mapping set: Serial4"):
                raise AssertionError(f"setup: ?MAP,SERIAL,S4,S0 printed {out}")
            mapped = True
            for kind in ("HCR", "MP3", "DFP"):
                out = _run(c2, _claim(kind, "S4", 2), 1.0)
                took[f"{kind} on the mapped S4"] = _has(out, TOOK[kind] + "4")
                if took[f"{kind} on the mapped S4"]:
                    _run(c2, f"?{kind},CLEAR", 1.0)
            _run(c2, "?MAP,SERIAL,CLEAR,S4", 1.0)
            mapped = False
            out = _run(c2, "?HCR,PORT,S4:9600", 1.0)
            if not _has(out, TOOK["HCR"] + "4"):
                raise AssertionError(f"setup: ?HCR,PORT,S4:9600 on the unmapped S4 printed {out}")
            out = _run(c2, "?MAP,SERIAL,S4,S0", 1.0)
            took["a mapping reading the HCR's S4"] = _has(out, "Serial mapping set: Serial4")
            if took["a mapping reading the HCR's S4"]:
                _run(c2, "?MAP,SERIAL,CLEAR,S4", 1.0)
            _run(c2, "?HCR,CLEAR", 1.0)
        finally:
            if mapped:
                _run(c2, "?MAP,SERIAL,CLEAR,S4", 1.0)
            _clear_all_w2(c2)
            _relabel(c2, before[2], "S4")
    bench.note(f"serial mapping against a device port: {took}")
    wrong = [k for k, v in took.items() if v]
    assert not wrong, f"accepted: {wrong}"


# ============================================================ HCR volume shadow and FN 20/21 (WCB-WP26 rows 4-5)
def _hcr_vols(c2):
    """(vV, vA, vB) and the rx count from ?HCR,STATUS, or (None, None)."""
    line = next((x for x in _run(c2, "?HCR,STATUS", 1.0) if x.startswith("[HCR:cfg=")), "")
    m = re.search(r"vV=(-?\d+),vA=(-?\d+),vB=(-?\d+),rx=(\d+)", line)
    return (tuple(int(v) for v in m.groups()[:3]), int(m.group(4))) if m else (None, None)


def _vol_replies(v):
    return f"<QVV,{v}>\n<QVA,{v}>\n<QVB,{v}>\n".encode()


@test("hcr.volume_shadow_seed_and_cache", "The HCR volume shadow: two agreeing QVx replies seed it (VOLUP then writes 75, not 55); a commanded volume shows in STATUS at once and STATUS transmits nothing; a reply already in flight after a write neither overwrites nor confirms it; after a fresh bind, disagreeing replies seed nothing", needs=["wcb1"], links=["W2S4"])
def volume_shadow_seed_and_cache(bench):
    """WCB-WP26 row 4. WcbHCR::onFrame (WCB_HCR.cpp:201-235) seeds HcrCodec's shadow from a QVx reply only while
    _hcrSeedOk (re-armed by hcrPoll when no query is in flight, :187-196; cleared by every volume write, :106-109),
    never mid-fade, and only when the channel's previous reply agreed (_hcrVolCand). A channel written since the last
    clean poll (_hcrVolCopied) keeps showing the commanded value and its reply is not counted (:217-224). ?HCR,REFRESH
    runs the same hcrPoll (:751-754) as the timed poll, one poll per command, so each reply set is placed exactly; the
    timed poll's seeding shows in hcr.config_softport's cadence switch. beginHCR resets the seeding state (:273-282)
    but not the codec's shadow (a module static, :99), so the plan's 'VOLUP gives 55' after a fresh bind holds only
    from a shadow of 50: VOL,50 goes first."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    probe = s4.probe
    problems = []

    def refresh(c2, label):
        pm = s4.mark()
        out = _run(c2, "?HCR,REFRESH", 1.0)
        if not _has(out, "[HCR] Refresh requested") or not probe.rule_hits(1, pm):
            problems.append(f"{label}: REFRESH printed {out}, reply rule hits {probe.rule_hits(1, pm)}")

    with _hcr_w2(bench) as c2:
        try:
            problems += _steps(s4, w.send, [(";H,VOL,50", _vol3(50))])
            s4.rule(1, POLL_HEX, _vol_replies(70), delay_ms=50)
            for k in range(3):
                refresh(c2, f"seeding poll {k + 1}")
            probe.rule_del(1)
            problems += _steps(s4, w.send, [(";H,VOLUP", _vol3(75)), (";H,VOL,A,20", _lf("<PVA20>"))])
            pm = s4.mark()
            vols, rx0 = _hcr_vols(c2)
            time.sleep(0.3)
            if vols != (75, 20, 75):
                problems.append(f"STATUS after VOLUP and VOL,A,20 shows vV/vA/vB {vols}, expected (75, 20, 75)")
            if s4.received(pm):
                problems.append(f"STATUS transmitted {s4.received(pm)!r}")
            s4.send(b"<QVA,70>\n")        # a reply that was already on the wire when VOL,A,20 went out
            time.sleep(0.6)
            vols, rx1 = _hcr_vols(c2)
            if rx0 is None or rx1 != rx0 + 1:
                problems.append(f"the injected <QVA,70> was not parsed (rx {rx0} -> {rx1})")
            if vols is None or vols[1] != 20:
                problems.append(f"the stale reply replaced the commanded vA=20: {vols}")
            problems += _steps(s4, w.send, [(";H,VOLUP,A", _lf("<PVA25>"))])
            # A fresh bind with replies that disagree (70, then 71): nothing is seeded.
            problems += _steps(s4, w.send, [(";H,VOL,50", _vol3(50))])
            m = c2.send("?HCR,PORT,S4:9600")
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=m)
            s4.rule(1, POLL_HEX, _vol_replies(70), delay_ms=50)
            refresh(c2, "fresh bind, poll 1")
            s4.rule(1, POLL_HEX, _vol_replies(71), delay_ms=50)
            refresh(c2, "fresh bind, poll 2")
            probe.rule_del(1)
            problems += _steps(s4, w.send, [(";H,VOLUP", _vol3(55))])
        finally:
            probe.rule_del(1)
    assert not problems, "; ".join(problems)


@test("hcr.fn_graceful_stops", ";H,FN,20 gives <PSG> and ;H,FN,21 gives <PSG>, <PSA,QPA>, <PSB,QPB>, whatever their parameters (the shared WcbCmd HcrCodec, which NaviCore also compiles)", needs=["wcb1"], links=["W2S4"])
def fn_graceful_stops(bench):
    """WCB-WP26 row 5. The FN branch (WCB_HCR.cpp:429-467) hands every fn but 14 to HcrCodec::emit; normalize()
    accepts 20 and 21 with any chan/track (WcbCmd WcbHcr.cpp:16-18) and format() emits them (:76-85). STOP,GRACEFUL and
    STOPEMOTE,GRACEFUL reach the same bytes through the HCR library (hcr.cpp:482-499), so these rows pin the codec
    NaviCore sends with. The comment above the branch (WCB_HCR.cpp:439-444) still says 'fns 2-13, 16, 17, 18, 19'."""
    graceful_all = _lf("<PSG>", "<PSA,QPA>", "<PSB,QPB>")
    bad, errs, lines = _routed_verbs(bench, [
        (";H,FN,20", _lf("<PSG>")), (";H,FN,21", graceful_all),
        (";H,FN,20,3,99", _lf("<PSG>")), (";H,FN,21,2,9999", graceful_all),
    ])
    assert not bad, "; ".join(bad)
    assert not errs, f"RXERR: {errs}"
    assert not lines, f"W2 printed HCR lines (a rejection?): {lines}"


# ============================================================ HCR, MP3 and DFP leftovers (WCB-WP52)
def _manual_route(bench, kind, cfg, cmd, frame, cap):
    """WCB-WP52 row 1 for ?MP3 / ?DFP -> problems. A route pinned by hand is stored and listed; CLEAR on a board with
    no local device keeps it; a host appearing does not override it (auto-join on); a pin to the host carries the verb
    there; and a pin on the host itself releases its device first."""
    s5 = link(bench, 2, "S5")
    name, verb = LEARNED[kind]
    w = usb_wcb(bench)
    _require_free(bench, 2, "S5")
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has MP3/DFP config")
    if "?WDP,AUTOJOIN,OFF" in bench.config_tokens(1):
        raise Skip("W1's auto-join is off, so it would learn no host whatever its route")
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        hosted = False
        try:
            out = [x.rstrip() for x in w.run(f"?{kind},REMOTE,W{NEVER_HEARD}")]
            if not _has(out, f"[{kind}] Routing ;{verb} to WCB{NEVER_HEARD}"):
                raise AssertionError(f"?{kind},REMOTE,W{NEVER_HEARD} printed {out}")
            if f"?{kind},REMOTE,W{NEVER_HEARD}" not in snapshot(bench, 1):
                problems.append("the pinned route is not in W1's chain")
            if not _has(w.run(f"?{kind},LIST"), f"  Routes ;{verb} to WCB{NEVER_HEARD} (remote host)"):
                problems.append(f"?{kind},LIST does not show the pin")
            out = [x.rstrip() for x in w.run(f"?{kind},CLEAR")]
            if not (_has(out, f"[{kind}] Local configuration cleared") and
                    _has(out, f"still routing ;{verb} to WCB{NEVER_HEARD}")) or _has(out, "✓"):
                problems.append(f"?{kind},CLEAR on a board that only routes printed {out}")
            wm = w.dev.mark()
            out = _run(c2, f"?{kind},{cfg}", 1.0)
            hosted = True
            if not _has(out, f"[{kind}] Configured: S5 at 9600 baud"):
                raise AssertionError(f"setup: ?{kind},{cfg} on W2 printed {out}")
            if not _await_cap(w, 2, cap, True):
                problems.append(f"W1 never saw W2 advertise its {name}, so the stored route was never tested")
            time.sleep(1.0)
            learned = [x for x in w.dev.since(wm) if "host learned" in x]
            if learned:
                problems.append(f"auto-learn overrode a stored route: {learned}")
            if f"?{kind},REMOTE,W{NEVER_HEARD}" not in snapshot(bench, 1):
                problems.append("W1's stored route changed when W2 started hosting")
            out = [x.rstrip() for x in w.run(f"?{kind},REMOTE,W2")]
            if not _has(out, f"[{kind}] Routing ;{verb} to WCB2"):
                problems.append(f"?{kind},REMOTE,W2 printed {out}")
            w.run("?DEBUG,ETM,ON")
            sm, wm = s5.mark(), w.dev.mark()
            w.send(cmd)
            try:
                s5.expect(frame, timeout=3, since=sm)
            except AssertionError:
                pass
            time.sleep(0.3)
            if s5.received(sm) != frame:
                problems.append(f"{cmd} put {s5.received(sm)!r} on W2 S5, expected {frame!r}")
            if _routes(w.dev.since(wm)) != [f"[ROUTE] {cmd} -> host WCB2"]:
                problems.append(f"W1 routed {_routes(w.dev.since(wm))}")
            w.run("?DEBUG,ETM,OFF")
            out = _run(c2, f"?{kind},REMOTE,W1", 1.0)
            hosted = False
            miss = _in_order_sub(out, [f"[{kind}] Local configuration cleared", "Re-enabled broadcast output on S5",
                                       "Re-enabled broadcast input on S5", "Reset S5 baud rate to 9600",
                                       "Serial5 label set to: ''", f"[{kind}] Routing ;{verb} to WCB1"])
            if miss:
                problems.append(f"?{kind},REMOTE,W1 on the host lacks {miss!r} (in order): {out}")
            toks = snapshot(bench, 2)
            problems += [f"W2's chain lacks {t}" for t in (f"?{kind},REMOTE,W1", "?BCAST,OUT,S5,ON", "?BCAST,IN,S5,ON")
                         if t not in toks]
            if [t for t in toks if t.upper().startswith(f"?{kind},S")]:
                problems.append("W2 kept its local device after pinning a route")
        finally:
            w.run("?DEBUG,ETM,OFF")
            if hosted:
                _run(c2, f"?{kind},CLEAR", 1.0)
            _run(c2, f"?{kind},REMOTE,OFF")
            _relabel(c2, before[2], "S5")
            _unlearn(bench, kind, learner=1)       # W1's route off, and nothing re-learns it
    return problems


@test("mp3.remote_route_manual", "?MP3,REMOTE,W<n> by hand: stored and listed, kept by ?MP3,CLEAR, not overridden when a host appears, carries ;A to the host (W2 S5); on the host it releases the local MP3 Trigger first", needs=["wcb1"], links=["W2S5"])
def mp3_remote_route_manual(bench):
    """WCB-WP52 row 1 (MP3). configureMP3's REMOTE branch (WCB_MP3.cpp:211-234) clears a local host first, then stores
    and saves the route; clearMP3Config keeps it (:145-165); mp3AutoAddRemote never overrides a stored host (:465-473);
    emitMP3Backup emits it (:398-406); routeStoredOrCap sends ;A there (WCB.ino:7764). The plan unlearns a fixture's
    route and turns auto-join off first; pinning before W2 hosts needs neither, and auto-join on is what makes 'never
    overridden' mean something."""
    problems = _manual_route(bench, "MP3", "S5:9600:V20", ";A,PLAY,5", bytes.fromhex("76147405"), CAP_MP3)
    assert not problems, "; ".join(problems)


@test("dfp.remote_route_manual", "?DFP,REMOTE,W<n> by hand: stored and listed, kept by ?DFP,CLEAR, not overridden when a host appears, carries ;D to the host (the 0x16 STOP frame on W2 S5); on the host it releases the local DFPlayer first", needs=["wcb1"], links=["W2S5"])
def dfp_remote_route_manual(bench):
    """WCB-WP52 row 1 (DFP). configureDFP's REMOTE branch (WCB_DFP.cpp:161-184), clearDFPConfig (:107-143),
    dfpAutoAddRemote (:425-433), emitDFPBackup (:358-366) and routeStoredOrCap's ;D arm (WCB.ino:7766-7768)."""
    problems = _manual_route(bench, "DFP", "S5", ";D,STOP", _dfp(0x16), CAP_DFP)
    assert not problems, "; ".join(problems)


@test("dfp.onerr_backup_and_clear", "?DFP,ONERR is saved right after ?DFP in the chain; ?DFP,ONERR,CLEAR removes it, and an error frame then recalls nothing", needs=["wcb1"], links=["W2S5", "W2S3"])
def dfp_onerr_backup_and_clear(bench):
    """WCB-WP52 row 2. emitDFPBackup puts ONERR right after the config token (WCB_DFP.cpp:368-378); ONERR,CLEAR empties
    onErrCmd (:190-193), and the codec's error callback then has nothing to recall (:49; WcbCmd WcbDfPlayer.cpp:209-212)."""
    s5, s3 = link(bench, 2, "S5"), link(bench, 2, "S3")
    _no_recall_keys(bench)
    ok, err = f"hilok{nonce().lower()}", f"hilerr{nonce().lower()}"
    problems = []
    with _w2_audio(bench, "DFP", "S5", "S5") as c2, _recall_keys(c2, "DFP", ok, err):
        tokens = snapshot(bench, 2)
        i = tokens.index("?DFP,S5:9600:V20") if "?DFP,S5:9600:V20" in tokens else -1
        if i < 0 or tokens[i + 1:i + 2] != ["?DFP,ONERR,HILE"]:
            problems.append(f"the chain does not hold ?DFP,S5:9600:V20^?DFP,ONERR,HILE: {[t for t in tokens if t.startswith('?DFP')]}")
        lines, got3 = _inject(c2, s5, s3, _dfp(0x40, 6))
        if not _has(lines, "[DFP] Error 0x06") or err.encode() + b"\r" not in got3:
            problems.append(f"before the clear the error frame did not recall HILE: {lines} / S3 {got3!r}")
        out = _run(c2, "?DFP,ONERR,CLEAR")
        if not _has(out, "[DFP] Error callback cleared"):
            problems.append(f"?DFP,ONERR,CLEAR printed {out}")
        if [t for t in snapshot(bench, 2) if t.startswith("?DFP,ONERR")]:
            problems.append("?DFP,ONERR,CLEAR left the ONERR token")
        if not _has(_run(c2, "?DFP,LIST"), "  On Error      : (none)"):
            problems.append("?DFP,LIST still shows an error callback")
        time.sleep(0.15)
        lines, got3 = _inject(c2, s5, s3, _dfp(0x40, 6))
        if not _has(lines, "[DFP] Error 0x06") or _has(lines, "Recalling") or got3:
            problems.append(f"after ONERR,CLEAR the error frame printed {lines} and put {got3!r} on S3")
    assert not problems, "; ".join(problems)


@test("dfp.port_move_release", "Re-issuing ?DFP on another port releases the old one (broadcast flags back on, label cleared, 'Released S4'), reserves the new one, and ;D follows the device", needs=["wcb1"], links=["W2S4", "W2S5"])
def dfp_port_move_release(bench):
    """WCB-WP52 row 3. configureDFP's move branch (WCB_DFP.cpp:283-299) re-enables the old port's flags, clears its
    label and prints 'Released', then the new port is reserved (:301-331). A DFPlayer is always 9600, so the old port's
    baud needs no reset here (an HCR or MP3 move does not reset it either, unlike their CLEAR)."""
    s4, s5 = link(bench, 2, "S4"), link(bench, 2, "S5")
    w = usb_wcb(bench)
    for p in ("S4", "S5"):
        _require_free(bench, 2, p)
    if _device_tokens(bench, 2) or _device_tokens(bench, 1, "MP3", "DFP"):
        raise Skip("W1 or W2 already has device config")
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        on = False
        try:
            wm = w.dev.mark()
            m = c2.send("?DFP,S4")
            on = True
            c2.expect(r"\[DFP\] Configured: S4 at 9600 baud", since=m)
            try:
                w.dev.expect(r"\[WDP\] DFPlayer host learned — routing ;D to WCB2", timeout=5, since=wm)
            except AssertionError:
                problems.append("W1 did not learn the DFPlayer host")
            m = c2.send("?DFP,S5")
            c2.expect(r"\[DFP\] Configured: S5 at 9600 baud", since=m)
            time.sleep(0.3)
            move = [x.rstrip() for x in c2.lines(m)]
            miss = _in_order_sub(move, ["Re-enabled broadcast output on S4 (old DFPlayer port)",
                                        "Re-enabled broadcast input on S4 (old DFPlayer port)", "Serial4 label set to: ''",
                                        "Released S4 (old DFPlayer port)", "Disabled broadcast output on S5 (DFPlayer port)",
                                        "Disabled broadcast input on S5 (DFPlayer port)", "Serial5 label set to: 'DFPlayer'",
                                        "[DFP] Configured: S5 at 9600 baud  default volume=20"])
            if miss:
                problems.append(f"the move lacks {miss!r} (in order): {move}")
            toks = snapshot(bench, 2)
            problems += [f"W2's chain lacks {t}" for t in ("?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON", "?BCAST,OUT,S5,OFF",
                                                           "?BCAST,IN,S5,OFF", "?LABEL,S5,DFPlayer", "?DFP,S5:9600:V20")
                         if t not in toks]
            if [t for t in toks if t.upper().startswith(("?LABEL,S4,", "?DFP,S4"))]:
                problems.append(f"S4 kept DFPlayer state: {[t for t in toks if t.upper().startswith(('?LABEL,S4,', '?DFP,S4'))]}")
            watch = Watch(s4, s5)
            w.send(";D,PLAY,5")
            try:
                watch.expect(s5, _dfp(0x03, 5), timeout=3)
            except AssertionError:
                problems.append(f";D,PLAY,5 did not follow the DFPlayer to S5: {watch.got(s5)!r}")
            time.sleep(0.5)
            if watch.got(s4):
                problems.append(f"bytes on the old DFPlayer port: {watch.got(s4)!r}")
        finally:
            if on:
                _run(c2, "?DFP,CLEAR", 1.0)
            _relabel(c2, before[2], "S4", "S5")
            if on:
                _unlearn(bench, "DFP", learner=1)
    assert not problems, "; ".join(problems)


def _w2_s1_shielded(bench):
    """Skip unless a broadcast cannot reach the real Maestro on W2 S1: its output flag is off, or it is a local Maestro
    port, which the broadcast writer skips whatever the flag says (WCB.ino:8300-8315)."""
    toks = bench.config_tokens(2, refresh=True)
    if "?BCAST,OUT,S1,OFF" in toks or any(re.match(r"^\?MAESTRO,M\d+:W2S1:", t, re.I) for t in toks):
        return
    raise Skip("a broadcast from W2 S3 would reach W2 S1, where the real Maestro is")


@test("mp3.bcast_skip_forced_on", "The MP3 Trigger port stays out of broadcasts even with ?BCAST,OUT forced back on, and what arrives on it never runs as a command", needs=["wcb1"], links=["W2S3", "W2S4", "W2S5"])
def mp3_bcast_skip_forced_on(bench):
    """WCB-WP52 row 4. The broadcast writer skips an MP3 Trigger port whatever its ?BCAST flag says (WCB.ino:8317-8323),
    and processIncomingSerial never reads it (:8404); the reservation turns the flags off as well (WCB_MP3.cpp:345-355),
    and here the output flag is forced back on. A plain line injected into W2 S3 is an ETM broadcast to every broadcast
    port on both WCBs, so it is a lowercase non-command marker; W2 S1 (the real Maestro) is skipped as a Maestro port."""
    s3, s4, s5 = link(bench, 2, "S3"), link(bench, 2, "S4"), link(bench, 2, "S5")
    require_tokens(bench, 2, "?BCAST,IN,S3,ON", "?BCAST,OUT,S4,ON")
    _w2_s1_shielded(bench)
    t = f"hilbc{nonce().lower()}".encode()
    with _w2_audio(bench, "MP3", "S5:9600:V20", "S5") as c2:
        c2.expect(r"Broadcast OUTPUT on S5: Enabled", since=c2.send("?BCAST,OUT,S5,ON"))
        watch = Watch(s3, s4, s5)
        s3.send(t + b"\r")
        watch.expect(s4, t + b"\r", timeout=3)
        time.sleep(1.5)
        got5, got3 = watch.got(s5), watch.got(s3)
        cm = c2.mark()
        watch = Watch(s4)
        s5.send(b";s4hil12\r")
        time.sleep(1.5)
        ran = watch.got(s4)
        printed = [x for x in c2.lines(cm) if x.strip() and not NOISE.search(x)]
    assert t not in got5, f"a broadcast reached the MP3 Trigger port with its output forced on: {got5!r}"
    assert t not in got3, "the source port echoed its own broadcast"
    assert b"hil12" not in ran and not printed, f"the MP3 port's RX ran as a command: S4 {ran!r}, console {printed}"


@test("hcr.poll_scheduler_edges", "With no volume replies the HCR poll repeats every 3 s ten times after the bind, then settles on POLL (10 s); a 2 s fade holds the poll, which resumes within 3 s of the fade's end (~75 s)", needs=["wcb1"], links=["W2S4"])
def poll_scheduler_edges(bench):
    """WCB-WP52 row 5. processHCRTick (WCB_HCR.cpp:293-318): while the three volumes are unseeded the next poll is
    min(POLL, 3) s away, at most _hcrFastPolls = 10 times per bind (:282, :314-317), then POLL; and a poll is held while
    a fade runs unless it is a whole POLL overdue (:299-307), because a fade step every 150 ms would land in the poll's
    reply window. hcr.config_softport covers the switch when replies do seed the volumes."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?HCR,REMOTE,W2")
    _require_free(bench, 2, "S4")
    if _device_tokens(bench, 2):
        raise Skip(f"W2 already has HCR/MP3/DFP config: {_device_tokens(bench, 2)}")
    poll = POLL.decode().rstrip("\n")
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        configured = False
        try:
            c2.expect(r"\[HCR\] Poll interval = 10s", since=c2.send("?HCR,POLL,10"))
            pm = s4.mark()
            m = c2.send("?HCR,PORT,S4:9600")
            configured = True
            c2.expect(r"\[HCR\] Configured on S4 at 9600 baud", since=m)
            time.sleep(53)
            stream, times = _frame_times(s4, pm, POLL)
            gaps = [b - a for a, b in zip(times, times[1:])]
            bench.note(f"unanswered poll gaps on the probe clock: {gaps}")
            if stream != POLL * len(times):
                problems.append(f"bytes other than the poll on W2 S4: {stream!r}")
            fast, slow = gaps[:10], gaps[10:]
            if len(fast) < 10 or not all(2950 <= g <= 3400 for g in fast):
                problems.append(f"the first ten gaps {fast} are not ~3 s")
            if len(slow) < 2 or not all(9950 <= g <= 10400 for g in slow):
                problems.append(f"after ten fast polls the gaps are {slow}, not ~10 s")
            c2.expect(r"\[HCR\] Poll interval = 3s", since=c2.send("?HCR,POLL,3"))
            m = s4.mark()
            w.send(";H,VOL,A,50")
            time.sleep(1.0)
            if s4.received(m).replace(POLL, b"") != _lf("<PVA50>"):
                problems.append(f"VOL,A,50 wrote {s4.received(m)!r}")
            m = s4.mark()
            w.send(";H,FADEOUT,A,2")
            time.sleep(6.0)
            frames = _timed_frames(s4, m)
            texts = [x for _, x in frames]
            bench.note(f"FADEOUT,A,2 under POLL,3: {frames}")
            first = next((i for i, x in enumerate(texts) if x.startswith("<PVA")), None)
            end = texts.index("<PSA,QPA>") if "<PSA,QPA>" in texts else None
            if first is None or end is None or end < first:
                problems.append(f"no fade ramp ending in <PSA,QPA>: {texts}")
            else:
                inside = [x for x in texts[first:end] if x == poll]
                if inside:
                    problems.append(f"{len(inside)} poll(s) went out during the fade: {texts}")
                after = [ms for ms, x in frames[end:] if x == poll]
                if not after:
                    problems.append(f"no poll after the fade: {texts}")
                elif after[0] - frames[end][0] > 3300:
                    problems.append(f"the first poll came {after[0] - frames[end][0]} ms after the fade's end")
        finally:
            if configured:
                m = c2.send("?HCR,CLEAR")
                c2.expect(r"  ✓ Released S4 \(old HCR port\)", since=m)
                _relabel(c2, before[2], "S4")
    assert not problems, "; ".join(problems)


@test("hcr.minor_verb_forms", ";H spellings no other test sends give their exact frames (VOLUME, OVERRIDE,1, MUSE,1, STOP,N, STOP,SOFT, STOPEMOTE,SOFT, MUSE,GAP with missing bounds); ?HCR,GET,EMOTION / PLAYING with a bad selector answer -1 and transmit nothing", needs=["wcb1"], links=["W2S4"])
def minor_verb_forms(bench):
    """WCB-WP52 row 6. VOLUME is VOL (WCB_HCR.cpp:574); OVERRIDE and MUSE take 1/ON for on and anything else for off
    (:524-542); hcrStopStyle maps N/HARD to NOW and G/SOFT to GRACEFUL (:382-387). MUSE,GAP reads its bounds with
    toInt(), so a missing one is 0 and the gap is sent, not refused (:533-537; the library only range-checks 0-99,
    hcr.cpp:459-465): characterisation. GET,EMOTION and GET,PLAYING print -1 for a selector that is not H/S/M/C or
    V/A/B, and only GET,VOL transmits (:810-834)."""
    s4 = link(bench, 2, "S4")
    w = usb_wcb(bench)
    problems = []
    with _hcr_w2(bench) as c2:
        pm, cm = s4.mark(), c2.mark()
        problems += _steps(s4, w.send, [
            (";H,VOLUME,A,40", _lf("<PVA40>")), (";H,volume,40", _vol3(40)),
            (";H,OVERRIDE,1", _lf("<O1,QO>")), (";H,OVERRIDE,OFF", _lf("<O0,QO>")),
            (";H,MUSE,1", _lf("<M1,QM>")), (";H,MUSE,0", _lf("<M0,QM>")),
            (";H,STOP,N", STOP_BURST), (";H,STOP,SOFT", _lf("<PSG>", "<PSA,QPA>", "<PSB,QPB>")),
            (";H,STOPEMOTE,SOFT", _lf("<PSG>")),
            (";H,MUSE,GAP", _lf("<MN0,MX0>")), (";H,MUSE,GAP,5", _lf("<MN5,MX0>")),
        ])
        errs = s4.errors(pm)
        lines = [x.rstrip() for x in c2.lines(cm) if x.startswith("[HCR]")]
        for field, want in (("EMOTION,X", "[HCR] EMOTION EMOTION,X = -1"), ("EMOTION", "[HCR] EMOTION EMOTION = -1"),
                            ("PLAYING,X", "[HCR] PLAYING PLAYING,X = -1")):
            m = s4.mark()
            out = _run(c2, f"?HCR,GET,{field}")
            if want not in out:
                problems.append(f"?HCR,GET,{field} printed {out}")
            if s4.received(m):
                problems.append(f"?HCR,GET,{field} transmitted {s4.received(m)!r}")
    assert not problems, "; ".join(problems)
    assert not errs, f"RXERR: {errs}"
    assert not lines, f"W2 printed HCR lines for the verbs: {lines}"


@test("mp3.playfs_callbacks", ";A,PLAYFS,<n>,ONFIN,<key> and ;A,PLAYFS,<n>,<key> play by SD index (76 14 70 nn) with a finish callback: X recalls it once, x cancels it", needs=["wcb1"], links=["W2S5", "W2S3"])
def mp3_playfs_callbacks(bench):
    """WCB-WP52 row 7. PLAYFS has its own setPending in the shared codec (WcbCmd WcbMp3.cpp:53-63): the key after
    ONFIN, or the bare field after the index, is recalled on the next 'X' and dropped on 'x' (:119-127, WCB_MP3.cpp:52)."""
    s5, s3 = link(bench, 2, "S5"), link(bench, 2, "S3")
    _no_recall_keys(bench)
    w = usb_wcb(bench)
    ok, err = f"hilok{nonce().lower()}", f"hilerr{nonce().lower()}"
    recall = f"Recalling command for key 'HILK': ;S3{ok}"
    problems = []
    with _w2_audio(bench, "MP3", "S5:9600:V20", "S5") as c2, _recall_keys(c2, "MP3", ok, err):
        try:
            for cmd, frame, label in ((";A,PLAYFS,7,ONFIN,HILK", "76147007", "ONFIN form"),
                                      (";A,PLAYFS,9,HILK", "76147009", "bare-key form")):
                problems += _steps(s5, w.send, [(cmd, bytes.fromhex(frame))])
                time.sleep(0.15)
                lines, got3 = _inject(c2, s5, s3, b"X")
                if not (_has(lines, "[MP3] Track finished") and _has(lines, recall) and ok.encode() + b"\r" in got3):
                    problems.append(f"{label}: X did not recall HILK: {lines} / S3 {got3!r}")
                lines, got3 = _inject(c2, s5, s3, b"X")
                if not _has(lines, "[MP3] Track finished") or _has(lines, "Recalling") or got3:
                    problems.append(f"{label}: a second X recalled again: {lines} / S3 {got3!r}")
            problems += _steps(s5, w.send, [(";A,PLAYFS,8,HILK", bytes.fromhex("76147008"))])
            time.sleep(0.15)
            lines, _ = _inject(c2, s5, s3, b"x", 0.4)
            if not _has(lines, "[MP3] Track cancelled"):
                problems.append(f"x: {lines}")
            lines, got3 = _inject(c2, s5, s3, b"X")
            if not _has(lines, "[MP3] Track finished") or _has(lines, "Recalling") or got3:
                problems.append(f"X after x recalled: {lines} / S3 {got3!r}")
        finally:
            s5.send(b"x")              # a key still pending is RAM in the codec: cancel it
            time.sleep(0.3)
    assert not problems, "; ".join(problems)
