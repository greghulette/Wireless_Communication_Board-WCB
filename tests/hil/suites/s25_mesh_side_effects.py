"""Two mesh behaviours nothing else reaches: WDP PWM self-heal after a MISSED explicit clear (docs/HIL_TEST_AUDIT.md
A17 / WP5) and W1's RC telemetry relay window (WP6).

The self-heal (reconcileWdpAutoPWMOutputs, WCB_PWM.cpp, called from the WDP advert decode in WCB_WDP.cpp) clears an
output a board auto-configured from a neighbour's PWMTARGET advert when a later advert from that neighbour no longer
names the port. On a healthy mesh the explicit ?MAP,PWM,CLEAR,OUT + ?REBOOT that W1 sends always wins, so the branch
never ran in any test. Here W2 is made deaf for the few seconds W1 sends that clear - its `?MAC,3` receive filter is
flipped on its own USB console, the same trick s18's `_deaf_w1` uses on W1 - and restored before W1's next advert.

Restore if aborted: on W2's console `?MAC,3,<its chain value>`, then `?MAP,PWM,CLEAR,OUT,S3` there (deferred reboot),
and `?MAP,PWM,CLEAR,ALL` on W1. Nothing here moves a servo.
"""
import re
import time

from hil.runner import Skip, test
from suites.common import Console, config_guard, link, require_tokens, snapshot, token, usb_wcb
from suites.s14_pwm import _clear_local_mapping, _clear_remote_out, _dump_cap, _has, _no_pwm, _pwm_reboot, _w2_reboot_wait


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

            # 2. W2 goes deaf: the explicit clear W1 is about to send never arrives.
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
