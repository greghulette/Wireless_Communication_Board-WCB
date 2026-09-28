"""WiFi modes and the WebSocket endpoint (docs/WIFI_DESIGN.md; docs/HIL_TEST_AUDIT.md WP7).
- Setter validation, no opt-in (docs/hil_plan/WCB.md WCB-WP17): refusals that save nothing, and saves that change
  nothing before a reboot - a derived SSID, passphrases ending in '?', holding commas, a leading or a trailing space.
  A mode or name is applied only at boot (wcbWifiStart, WCB.ino:9619), and these tests never reboot: each replays W1's
  own ?WIFI line in its finally and checks the chain holds it again.
- `wifi_modes` (opt-in): W1 turns its access point off and back on, joins W2's access point and returns to its own,
  and hosts its access point under the derived name for one boot. A mode applies at boot, so each change is a W1 reboot
  (six in all). Nothing needs the PC's WiFi adapter. JOIN robustness (WCB-WP45) adds two more under the same key: W1
  losing W2's access point and joining it again (W2's own access point off and back: two W2 reboots), and W1 looking
  for a network nobody hosts for 70 s without leaving the mesh channel (four more W1 reboots in all).
- `wifi_pc` (attended): a WiFi adapter on the PC joins W1's access point, opens ws://192.168.4.1/ws and gets ?VERSION
  answered over it, then returns to the network it was on. It uses an adapter that does not carry the PC's internet
  when there is one (this bench's TP-Link "Wi-Fi 2"); with a single adapter the PC is offline for about 30 s.

Credentials: the AP and JOIN lines carry a password. It is read from the board's own chain at run time and given
back to a board or to a Windows WiFi profile that is removed afterwards; it never goes into a note, a message or a
file that stays. (?backup output, and so the session log, already carries it, as it always has.) Neither does an SSID:
W1's own or derived name is compared, never quoted, and a token is shown only as its hash (redact_token). The setter
tests' throwaway SSIDs and passphrases (HIL + a nonce) are never used for a network. A restore if aborted: replay W1's
own ?WIFI line from its chain on its USB console (and reboot, if the board was rebooted since it changed); on the PC,
`netsh wlan delete profile name=HIL-<W1's SSID> interface=<adapter>` and `netsh wlan connect name=<its network>
interface=<adapter>`.
"""
import os
import re
import subprocess
import tempfile
import time

from hil.checkpoint import redact_text, redact_token
from hil.runner import Skip, test
from hil.wcb import WCB
from hil.ws import WsClient
from suites.common import Console, Watch, config_guard, link, marker, nonce, snapshot, token, usb_wcb
from suites.s03_wcb import _w2_online


def _has(lines, text):
    return any(text in x for x in lines)


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _wifi_token(tokens):
    """(the whole ?WIFI token, MODE, ssid, password) from a chain. The password is only ever handed back to a board."""
    t = token(tokens, "?WIFI,")
    if t is None:
        raise Skip("the chain lacks a ?WIFI line")
    parts = t.split(",")
    mode = parts[1].upper()
    ssid = parts[2] if len(parts) > 2 else ""
    pw = ",".join(parts[3:]) if len(parts) > 3 else ""
    return t, mode, ssid, pw


def _status(w):
    """The ?WIFI block as {label: value} ('Mode', 'Interface', 'IP address', 'Radio channel', 'WS endpoint', ...)."""
    out = {}
    for x in w.run("?WIFI"):
        m = re.match(r"^([A-Za-z][A-Za-z ]*?)\s+: (.*)$", x.rstrip())
        if m:
            out[m.group(1)] = m.group(2)
    if "Mode" not in out:
        raise AssertionError("?WIFI printed no status block")
    return out


def _mesh_channel(tokens):
    t = token(tokens, "?WCBCH,")
    if t is None:
        raise Skip("the chain lacks ?WCBCH")
    return t.split(",")[1]


def _unicast_ok(w, w2s2, problems, when):
    t = marker()
    watch = Watch(w2s2)
    w.send(f";W2,;S2{t}")
    try:
        watch.expect(w2s2, t.encode(), timeout=5)
    except AssertionError:
        problems.append(f"a unicast to W2 was not delivered {when}")


def _restore_ap(w, tok, ssid, problems):
    """Replay W1's own ?WIFI,AP line and reboot; the SoftAP boot line proves it is back."""
    out = w.run(tok)
    if not _has(out, f'WiFi mode set to AP — SSID "{ssid}"'):
        problems.append("replaying W1's ?WIFI,AP line did not confirm")
    bm = w.reboot()
    if not _has(w.dev.since(bm), f'[WIFI] SoftAP "{ssid}" up on channel'):
        problems.append("W1's access point did not come back at boot")


# ============================================================ setter validation (no opt-in: nothing applies before a reboot)
def _derived_ssid(tokens, n):
    """The name wcbWifiDefaultSsid() builds from the chain: 'WCB-<alias>', or 'WCB-<n>' with no alias, cut to 32
    characters (WCB_WiFi.cpp:71-79). An SSID: compared, never printed."""
    a = token(tokens, "?ALIAS,")
    return ("WCB-" + (a.split(",", 1)[1] if a else str(n)))[:32]


def _wifi_now(bench):
    """W1's ?WIFI token as its chain holds it now, or None."""
    return token(snapshot(bench, 1), "?WIFI,")


def _put_back(w, bench, tok, problems):
    """Replay W1's own ?WIFI line (never printed) and check its chain holds it again. A save only applies at the next
    boot (wcbWifiStart, WCB.ino:9619), and these tests never reboot, so the access point itself is never touched."""
    w.run(tok)
    now = _wifi_now(bench)
    if now != tok:
        problems.append(f"W1's ?WIFI token did not come back ({redact_token(now or '(none)')}, expected "
                        f"{redact_token(tok)}): replay W1's own ?WIFI line from its ?backup on its USB console before "
                        f"anything reboots it")


@test("wifi.setter_refusals", "?WIFI refuses and saves nothing for an AP password under 8 characters or none at all (the open-AP guard), JOIN with no network name, an SSID over 32 characters (AP and JOIN) and an unknown verb; W1's Mode line and ?WIFI token stay as they were (no opt-in: nothing is saved)", needs=["wcb1"], links=[])
def setter_refusals(bench):
    """WCB-WP17 row 1. processWifiCommand (WCB_WiFi.cpp:373-421) checks the verb, the JOIN network name, the SSID length
    and the AP password before it stores anything (:423-432), so a refusal saves nothing and W1's access point is never
    touched. The SSIDs and passphrases here are throwaway; W1's own token is compared by hash, and replayed at once
    should a refusal ever regress into a save."""
    w = usb_wcb(bench)
    long_ssid = "HIL" + "X" * 30                                 # 33 characters
    open_ap = ["Refusing to configure an OPEN access point — this interface accepts",
               "commands for the whole mesh with no credential of its own."]
    join_name = ["A network name is required: ?WIFI,JOIN,<ssid>,<pass>"]
    too_long = ["SSID is 33 characters; the maximum is 32."]
    checks = (
        ("an AP password of 7 characters", "?WIFI,AP,HILX,abc1234",
         ["AP password is 7 character(s); WPA2 requires at least 8."] + open_ap),
        ("an AP with no password", "?WIFI,AP,HILX", ["AP password is 0 character(s); WPA2 requires at least 8."] + open_ap),
        ("JOIN with no network", "?WIFI,JOIN", join_name),
        ("JOIN with an empty network name", "?WIFI,JOIN,,hilpass99", join_name),
        ("JOIN with a 33-character SSID", f"?WIFI,JOIN,{long_ssid},hilpass99", too_long),
        ("an AP with a 33-character SSID", f"?WIFI,AP,{long_ssid},hilpass99", too_long),
        ("an unknown verb", "?WIFI,BOGUS", ["Usage: ?WIFI | ?WIFI,OFF | ?WIFI,AP,<ssid>,<pass> | ?WIFI,JOIN,<ssid>,<pass>"]),
    )
    problems = []
    with config_guard(bench, 1) as before:
        tok = token(before[1], "?WIFI,")
        if tok is None:
            raise Skip("the chain lacks a ?WIFI line")
        mode0 = _status(w).get("Mode")
        try:
            for what, cmd, wants in checks:
                out = [x.rstrip() for x in w.run(cmd)]
                missing = [x for x in wants if x not in out]
                if missing:
                    problems.append(f"{what}: no {missing}")
                if _has(out, "WiFi mode set to"):
                    problems.append(f"{what} was saved")
            mode = _status(w).get("Mode")
            if mode != mode0:
                problems.append(f"the Mode line went from {mode0!r} to {mode!r}")
        finally:
            if _wifi_now(bench) != tok:
                problems.append("a refused ?WIFI line changed W1's ?WIFI token")
                _put_back(w, bench, tok, problems)
    assert not problems, "; ".join(problems)


@test("wifi.ap_derived_ssid", "?WIFI,AP with an empty SSID and W1's own password saves the derived name WCB-<alias> (WCB-<n> with no alias): the confirmation names it, the status block shows it as '(derived)', the chain token's SSID is empty, and the running access point is untouched; W1's own ?WIFI line goes back at once (no opt-in: a name applies only at boot, and nothing boots here)", needs=["wcb1"], links=[])
def ap_derived_ssid(bench):
    """WCB-WP17 row 2. Only JOIN needs a network name (WCB_WiFi.cpp:404-407): an AP's empty SSID is stored as it is,
    and the confirmation and the status block show wcbWifiDefaultSsid() instead (:434-443, :337-339; the name itself at
    :71-79). A mode is applied only at boot (wcbWifiStart, WCB.ino:9619), so the access point keeps running under its own
    name until W1's own line is replayed. The boot half, the SoftAP coming up under the derived name, is
    wifi.ap_derived_ssid_boot (opt-in wifi_modes). The SSIDs and the password are compared, never printed."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        tok, mode, ssid, pw = _wifi_token(before[1])
        if mode != "AP" or len(pw) < 8:
            raise Skip("W1 does not host an access point with a password")
        derived = _derived_ssid(before[1], bench.usb_wcb_number())
        st0 = _status(w)
        changed = False
        try:
            out = [x.rstrip() for x in w.run(f"?WIFI,AP,,{pw}")]
            changed = True
            if not any(x.startswith(f'WiFi mode set to AP — SSID "{derived}" on channel ') for x in out):
                problems.append("the confirmation does not name the derived SSID (WCB- and the alias, or the number)")
            st = _status(w)
            if st.get("AP SSID") != f"{derived} (derived)":
                problems.append("the status block's AP SSID is not the derived name marked '(derived)'")
            if st.get("AP password") != "set":
                problems.append(f"the status block's AP password line says {st.get('AP password')!r}")
            for k in ("Mode", "Interface", "IP address", "Radio channel"):
                if st.get(k) != st0.get(k):
                    problems.append(f"the saved name changed the running {k} line: {st0.get(k)!r} -> {st.get(k)!r}")
            if _wifi_now(bench) != f"?WIFI,AP,,{pw}":
                problems.append("the chain token is not ?WIFI,AP with an empty SSID and W1's own password")
        finally:
            if changed:
                _put_back(w, bench, tok, problems)
    assert not problems, "; ".join(problems)


@test("wifi.passphrase_trailing_q", "A ?WIFI passphrase ending in '?' is saved, not eaten as a help request, in the upper- and the mixed-case spelling (?Wifi, tracker #95): each confirms, prints no help, and the chain token ends in the '?'; W1's own ?WIFI line goes back at once (no opt-in: nothing applies before a reboot)", needs=["wcb1"], links=[])
def passphrase_trailing_q(bench):
    """WCB-WP17 row 3. A '?' command whose body ends in '?' is a help request (WCB.ino:6055-6059), but the data-bearing
    verbs, WIFI among them and matched in any case since tracker #95 (docs/HIL_WEEK_DECISIONS.md D31), are exempt
    (:6036-6053), so the line reaches processWifiCommand and saveWifiSettings (WCB_WiFi.cpp:423-432). Throwaway SSID and
    passphrases; W1's own token is compared by hash and replayed in the finally."""
    w = usb_wcb(bench)
    s = f"HIL{nonce()}"
    problems = []
    with config_guard(bench, 1) as before:
        tok = token(before[1], "?WIFI,")
        if tok is None:
            raise Skip("the chain lacks a ?WIFI line")
        changed = False
        try:
            for verb in ("?WIFI", "?Wifi"):
                p = f"hil{nonce().lower()}?"
                changed = True
                out = [x.rstrip() for x in w.run(f"{verb},AP,{s},{p}")]
                if _has(out, "Command Reference") or not any(x.startswith(f'WiFi mode set to AP — SSID "{s}"') for x in out):
                    problems.append(f"{verb},AP with a passphrase ending in '?' was not saved ({len(out)} line(s), no "
                                    f"confirmation{', the help page' if _has(out, 'Command Reference') else ''})")
                if _wifi_now(bench) != f"?WIFI,AP,{s},{p}":
                    problems.append(f"after {verb},AP the chain token is not the throwaway line ending in '?'")
        finally:
            if changed:
                _put_back(w, bench, tok, problems)
    assert not problems, "; ".join(problems)


@test("wifi.passphrase_commas_spaces", "A ?WIFI passphrase is everything after the SSID: its commas stay and so does a leading space, while a trailing space is trimmed off the line before ?WIFI sees it (the documented rule since re-scan #31); each is read back from the chain token; W1's own ?WIFI line goes back at once (no opt-in: nothing applies before a reboot)", needs=["wcb1"], links=[])
def passphrase_commas_spaces(bench):
    """WCB-WP17 row 4. processWifiCommand splits only at the verb's and the SSID's commas and leaves the passphrase
    untrimmed (WCB_WiFi.cpp:376-399); every line reader trims the whole line first (processIncomingSerial, WCB.ino:8445),
    and the comment at WCB_WiFi.cpp:400-402 records that as the rule (re-scan #31, docs/HIL_WEEK_DECISIONS.md D15). AP,
    not JOIN as the plan has it: the split is the same code for both verbs, and AP leaves W1's saved mode as it is.
    Throwaway SSID and passphrases (each 8+ characters, so the AP check passes); W1's own token is compared by hash and
    replayed in the finally."""
    w = usb_wcb(bench)
    s, n = f"HIL{nonce()}", nonce().lower()
    cases = (("commas", f"ab,cd,ef{n}", f"ab,cd,ef{n}"),
             ("a leading space", f" lead{n}", f" lead{n}"),
             ("a trailing space", f"trail{n} ", f"trail{n}"))
    problems = []
    with config_guard(bench, 1) as before:
        tok = token(before[1], "?WIFI,")
        if tok is None:
            raise Skip("the chain lacks a ?WIFI line")
        changed = False
        try:
            for what, sent, stored in cases:
                changed = True
                out = [x.rstrip() for x in w.run(f"?WIFI,AP,{s},{sent}")]
                if not any(x.startswith(f'WiFi mode set to AP — SSID "{s}"') for x in out):
                    problems.append(f"a passphrase with {what} was not saved")
                    continue
                now = _wifi_now(bench) or ""
                if now != f"?WIFI,AP,{s},{stored}":
                    got = now.split(",", 3)[3] if now.count(",") >= 3 else ""
                    problems.append(f"a passphrase with {what} was stored as {len(got)} characters "
                                    f"({redact_token(now)}), expected {len(stored)}")
        finally:
            if changed:
                _put_back(w, bench, tok, problems)
    assert not problems, "; ".join(problems)


# ============================================================ modes (no PC adapter)
@test("wifi.off_and_back", "?WIFI,OFF takes W1's access point down at the next boot (OFF status, interface down, no WS endpoint, no SoftAP boot line, radio still on the mesh channel) while the mesh keeps delivering; W1's own ?WIFI,AP line and a reboot bring it back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes")
def off_and_back(bench):
    """processWifiCommand / wcbWifiBegin (WCB_WiFi.cpp): a mode is saved to NVS (the Mode line shows it at once) and
    applied at boot only, so the interface stays as it was until the reboot; with WiFi off ESP-NOW still pins the
    radio to the mesh channel."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        tok, mode, ssid, _ = _wifi_token(before[1])
        if mode != "AP":
            raise Skip("W1 does not host an access point")
        ch = _mesh_channel(before[1])
        changed = False
        try:
            out = w.run("?WIFI,OFF")
            changed = True
            if not _has(out, "WiFi disabled — reboot to apply."):
                problems.append("?WIFI,OFF did not confirm")
            st = _status(w)
            if not st.get("Mode", "").startswith("OFF") or st.get("Interface") != "up":
                problems.append(f"right after ?WIFI,OFF the block should show the saved mode with the AP still up: "
                                f"Mode {st.get('Mode')!r}, Interface {st.get('Interface')!r}")
            bm = w.reboot()
            if _has(w.dev.since(bm), "[WIFI] SoftAP"):
                problems.append("WiFi is off, yet the boot brought a SoftAP up")
            st = _status(w)
            for k, want in (("Mode", "OFF (ESP-NOW only)"), ("Interface", "down"), ("WS endpoint", "NOT RUNNING")):
                if st.get(k) != want:
                    problems.append(f"with WiFi off, {k} is {st.get(k)!r}, expected {want!r}")
            radio = st.get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"with WiFi off the radio line is {radio!r}")
            _unicast_ok(w, w2s2, problems, "with WiFi off")
        finally:
            if changed:
                _restore_ap(w, tok, ssid, problems)
        st = _status(w)
        if st.get("Mode") != "AP" or st.get("Interface") != "up" or not st.get("WS endpoint", "").startswith("ws://"):
            problems.append(f"after the restore: Mode {st.get('Mode')!r}, Interface {st.get('Interface')!r}, WS {st.get('WS endpoint')!r}")
    assert not problems, "; ".join(problems)


@test("wifi.join_w2_ap", "W1 joins W2's access point: JOIN saved for the next boot, then W1 associates on the mesh channel (boot lines, the ?WIFI JOIN block with an address and a WS endpoint, W2 counting a client) and the mesh still delivers; W1's own access point is put back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes")
def join_w2_ap(bench):
    """wcbWifiJoinTry pins the association to meshChannel, and the JOIN state machine verifies the radio stayed there
    (WCB_WiFi.cpp). W2's AP is on the same mesh channel, so the join is expected to settle 'connected'."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        tok1, mode1, ssid1, _ = _wifi_token(before[1])
        tok2, mode2, ssid2, pw2 = _wifi_token(before[2])
        if mode1 != "AP" or mode2 != "AP":
            raise Skip("W1 and W2 must both host an access point (W2's is joined, W1's is put back)")
        if not pw2:
            raise Skip("W2's ?WIFI,AP line carries no password")
        ch = _mesh_channel(before[1])
        if _mesh_channel(before[2]) != ch:
            raise Skip("W1 and W2 are on different mesh channels")
        changed = False
        try:
            out = w.run(f"?WIFI,JOIN,{ssid2},{pw2}")
            changed = True
            if not _has(out, f'WiFi mode set to JOIN — joining "{ssid2}"'):
                problems.append("?WIFI,JOIN did not confirm")
            bm = w.reboot()
            if not _has(w.dev.since(bm), f'[WIFI] will join "{ssid2}" on channel {ch}'):
                problems.append("no 'will join' boot line")
            try:
                w.dev.expect(rf'^\[WIFI\] joined "{re.escape(ssid2)}" on channel {ch} after \d+ attempt\(s\) — ws://\S+/ws$',
                             timeout=45, since=bm)
            except AssertionError:
                problems.append("W1 did not report joining W2's access point within 45 s of booting")
            st = _status(w)
            if st.get("Mode") != "JOIN" or st.get("Join SSID") != ssid2:
                problems.append(f"Mode {st.get('Mode')!r}, Join SSID {st.get('Join SSID')!r}")
            if not (st.get("Association", "").startswith("connected (") and st.get("Interface") == "up"):
                problems.append(f"Association {st.get('Association')!r}, Interface {st.get('Interface')!r}")
            if not st.get("IP address", "").startswith("192.168.4."):
                problems.append(f"no address from W2's AP: {st.get('IP address')!r}")
            radio = st.get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"joined, the radio line is {radio!r}")
            if not st.get("WS endpoint", "").startswith("ws://192.168.4."):
                problems.append(f"WS endpoint {st.get('WS endpoint')!r}")
            clients = next((x for x in _crun(c2, "?WIFI", 1.5) if x.startswith("Clients       : ")), "")
            if not re.match(r"^Clients       : [1-9]", clients):
                problems.append(f"W2 counts no client while W1 is joined: {clients!r}")
            _unicast_ok(w, w2s2, problems, "while joined to W2's AP")
        finally:
            if changed:
                _restore_ap(w, tok1, ssid1, problems)
        if _status(w).get("Mode") != "AP":
            problems.append("W1 is not back in AP mode")
    assert not problems, "; ".join(problems)


@test("wifi.ap_derived_ssid_boot", "An access point saved with an empty SSID comes up at boot under the derived name WCB-<alias> (the SoftAP boot line, and '(derived)' in the status block) while the mesh still delivers; W1's own ?WIFI,AP line and a reboot bring its own name back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes", opt_in_why="changes W1's access point name and reboots it twice")
def ap_derived_ssid_boot(bench):
    """WCB-WP17 row 2, the boot half (wifi.ap_derived_ssid is the rest, with no reboot). wcbWifiStartAP hosts
    wcbWifiDefaultSsid() when the saved SSID is empty (WCB_WiFi.cpp:108, :71-79) and says so at boot:
    '[WIFI] SoftAP "<ssid>" up on channel <n> - ws://...' (:180). Anything on W1's access point drops for the ~20 s the
    other name is up; nothing on this bench uses it outside wifi.pc_joins_ap_ws. The derived name and W1's own are
    compared, never printed."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        tok, mode, ssid, pw = _wifi_token(before[1])
        if mode != "AP" or len(pw) < 8:
            raise Skip("W1 does not host an access point with a password")
        if not ssid:
            raise Skip("W1's access point already uses the derived name")
        derived = _derived_ssid(before[1], bench.usb_wcb_number())
        changed = False
        try:
            out = w.run(f"?WIFI,AP,,{pw}")
            changed = True
            if not _has(out, f'WiFi mode set to AP — SSID "{derived}"'):
                problems.append("?WIFI,AP with an empty SSID did not confirm the derived name")
            bm = w.reboot()
            if not _has(w.dev.since(bm), f'[WIFI] SoftAP "{derived}" up on channel'):
                problems.append("the boot did not bring the access point up under the derived name")
            st = _status(w)
            if st.get("AP SSID") != f"{derived} (derived)" or st.get("Interface") != "up":
                problems.append(f"after the boot the status block's AP SSID is not the derived name, or the interface "
                                f"is {st.get('Interface')!r}")
            if not st.get("WS endpoint", "").startswith("ws://"):
                problems.append(f"WS endpoint {st.get('WS endpoint')!r}")
            _unicast_ok(w, w2s2, problems, "with the access point under the derived name")
        finally:
            if changed:
                _restore_ap(w, tok, ssid, problems)
        st = _status(w)
        if st.get("AP SSID") != ssid or st.get("Interface") != "up":
            problems.append(f"W1's access point did not come back under its own name (interface {st.get('Interface')!r})")
    assert not problems, "; ".join(problems)


# ============================================================ JOIN robustness (WCB-WP45, opt-in wifi_modes)
def _w2_ap_back(w2, tok2, ssid2, problems):
    """Replay W2's own ?WIFI,AP line on its own console and reboot it; the SoftAP boot line proves it is back. The
    SSID is compared, never quoted."""
    if not _has(w2.run(tok2), f'WiFi mode set to AP — SSID "{ssid2}"'):
        problems.append("replaying W2's ?WIFI,AP line did not confirm")
    bm = w2.reboot()
    if not _has(w2.dev.since(bm), f'[WIFI] SoftAP "{ssid2}" up on channel'):
        problems.append("W2's access point did not come back at boot")


@test("wifi.join_lost_and_rejoin", "W1 joined to W2's access point notices it going away ('lost ... retrying every 5 s') while W2 boots with WiFi off, and joins it again by itself ('joined ... after N attempt(s)') once it is back; ?WIFI then shows the association connected and a unicast to W2 is delivered; both access points are put back (W1 x2, W2 x2 reboots)", needs=["wcb1", "wcb2"], links=["W2S2"], opt_in="wifi_modes", opt_in_why="joins W1 to W2's access point, turns W2's access point off and back on, and reboots each board twice")
def join_lost_and_rejoin(bench):
    """WCB-WP45 row 1 (wifi.join_lost_and_rejoin). A JOIN that loses its association prints 'lost "<ssid>" - retrying
    every 5 s' once and falls through to the retry, never latching (WCB_WiFi.cpp:239-289), and the next association
    prints 'joined ... after N attempt(s)' (:261-267), N counting every attempt since boot. The plan rebooted W2 to
    take its access point away, but a reboot has it back within a few seconds, under the station's beacon timeout, so
    W1 might never see the loss; W2 boots with WiFi off instead, and its own ?WIFI,AP line and a reboot bring the
    access point back (the same pair wifi.off_and_back runs on W1). W2's SSID and passphrase are read from its chain,
    handed to W1 and back, and never quoted."""
    w2s2 = link(bench, 2, "S2")
    w, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    problems, facts = [], {}
    with config_guard(bench, 1, 2) as before:
        tok1, mode1, ssid1, _ = _wifi_token(before[1])
        tok2, mode2, ssid2, pw2 = _wifi_token(before[2])
        if mode1 != "AP" or mode2 != "AP":
            raise Skip("W1 and W2 must both host an access point (W2's is joined, W1's is put back)")
        if not pw2:
            raise Skip("W2's ?WIFI,AP line carries no password")
        ch = _mesh_channel(before[1])
        if _mesh_channel(before[2]) != ch:
            raise Skip("W1 and W2 are on different mesh channels")
        joined = rf'^\[WIFI\] joined "{re.escape(ssid2)}" on channel {ch} after (\d+) attempt\(s\) \S+ ws://\S+/ws$'
        changed1 = changed2 = False
        try:
            out = w.run(f"?WIFI,JOIN,{ssid2},{pw2}")
            changed1 = True
            if not _has(out, f'WiFi mode set to JOIN — joining "{ssid2}"'):
                problems.append("?WIFI,JOIN did not confirm")
            bm = w.reboot()
            try:
                w.dev.expect(joined, timeout=45, since=bm)
            except AssertionError:
                raise AssertionError("W1 did not join W2's access point within 45 s of booting") from None
            lm = w.dev.mark()
            if not _has(w2.run("?WIFI,OFF"), "WiFi disabled — reboot to apply."):
                problems.append("W2 did not take ?WIFI,OFF")
            changed2 = True
            w2.reboot()
            try:
                w.dev.expect(rf'^\[WIFI\] lost "{re.escape(ssid2)}" \S+ retrying every 5 s$', timeout=30, since=lm)
                facts["lost"] = True
            except AssertionError:
                problems.append("W1 printed no 'lost ... retrying every 5 s' within 30 s of W2 turning its access point off")
                facts["lost"] = False
            rm = w.dev.mark()
            _w2_ap_back(w2, tok2, ssid2, problems)
            changed2 = False
            try:
                facts["attempts"] = int(w.dev.expect(joined, timeout=45, since=rm).group(1))
            except AssertionError:
                problems.append("W1 did not join W2's access point again within 45 s of it coming back")
            _w2_online(bench, w)
            st = _status(w)
            if not (st.get("Association", "").startswith("connected (") and st.get("Interface") == "up"):
                problems.append(f"after the rejoin: Association {st.get('Association')!r}, Interface {st.get('Interface')!r}")
            radio = st.get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"after the rejoin the radio line is {radio!r}")
            _unicast_ok(w, w2s2, problems, "after the rejoin")
        finally:
            if changed2:
                _w2_ap_back(w2, tok2, ssid2, problems)
            if changed1:
                _restore_ap(w, tok1, ssid1, problems)
        if _status(w).get("Mode") != "AP":
            problems.append("W1 is not back in AP mode")
    bench.note(f"wifi.join_lost_and_rejoin: {facts}")
    assert not problems, "; ".join(problems)


RETRY_ALLOWANCE = 2     # ETM retries a pinned-channel JOIN may cost in 70 s: radio noise, not a channel sweep


@test("wifi.join_absent_ssid_keeps_mesh", "A JOIN to a network nobody hosts retries for ever on the mesh channel without sweeping: 'will join', 'still looking (attempt 1)', and '(attempt 12)' about 55 s later, while a unicast to W2 every 5 s for 70 s arrives with no failure and at most two retries and the ?WIFI radio line stays on the mesh channel; W1's own access point is put back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes", opt_in_why="points W1's JOIN at a network nobody hosts for about 70 s and reboots W1 twice")
def join_absent_ssid_keeps_mesh(bench):
    """WCB-WP45 row 2 (wifi.join_absent_ssid_keeps_mesh). wcbWifiJoinTry passes meshChannel to WiFi.begin and keeps
    auto-reconnect off, so the probe for the SSID stays on the channel the mesh already uses (WCB_WiFi.cpp:191-202),
    and the state machine retries every JOIN_RETRY_MS (5 s, :36) for ever, saying so at attempt 1 and every 12th
    (:279-287). A channel sweep would take the radio off the mesh for most of a second every 5 s, and W1's unicasts
    would need retries or fail. The SSID and passphrase are throwaway (HIL + a nonce, never a network); W1's own ?WIFI
    line is replayed at the end."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems, radios, facts = [], [], {}
    with config_guard(bench, 1) as before:
        tok, mode, ssid, _ = _wifi_token(before[1])
        if mode != "AP":
            raise Skip("W1 does not host an access point")
        ch = _mesh_channel(before[1])
        nosuch, pw_x = f"HIL-NOSUCH-{nonce()}", f"hil{nonce().lower()}pass"
        looking = rf'^\[WIFI\] still looking for "{re.escape(nosuch)}" \(attempt {{}}\)$'
        changed = False
        try:
            out = w.run(f"?WIFI,JOIN,{nosuch},{pw_x}")
            changed = True
            if not _has(out, f'WiFi mode set to JOIN — joining "{nosuch}"'):
                problems.append("?WIFI,JOIN to the absent network did not confirm")
            bm = w.reboot()
            if not _has(w.dev.since(bm), f'[WIFI] will join "{nosuch}" on channel {ch}'):
                problems.append("no 'will join ... on channel <mesh channel>' boot line")
            try:
                w.dev.expect(looking.format(1), timeout=20, since=bm)
            except AssertionError:
                raise AssertionError("no 'still looking ... (attempt 1)' line within 20 s of the boot") from None
            base = w.etm_board_stats().get(2) or {}
            t0 = time.monotonic()
            n = 0
            while time.monotonic() - t0 < 70:
                n += 1
                _unicast_ok(w, w2s2, problems, f"(unicast {n}) while W1 looked for the absent network")
                radio = _status(w).get("Radio channel", "")
                if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                    radios.append(radio)
                time.sleep(max(0.0, t0 + 5 * n - time.monotonic()))
            after = w.etm_board_stats().get(2) or {}
            lines = w.dev.since(bm)
            facts.update(unicasts=n, stats_before=base, stats_after=after)
            if not any(re.match(looking.format(12), x) for x in lines):
                problems.append("no 'still looking ... (attempt 12)' line within 70 s of attempt 1")
            if any(re.match(looking.format(k), x) for x in lines for k in (2, 3, 13)):
                problems.append("'still looking' printed for an attempt other than 1 and every 12th")
            if any("[ETM] WCB2 went OFFLINE" in x for x in lines):
                problems.append("W1 marked W2 offline while it looked for the absent network")
            if not base or not after:
                problems.append("?STATS has no WCB2 row")
            else:
                failed, retries = after["failed"] - base["failed"], after["retries"] - base["retries"]
                if failed or retries > RETRY_ALLOWANCE:
                    problems.append(f"unicasts to W2 while looking: {failed} failed, {retries} retries (at most "
                                    f"{RETRY_ALLOWANCE} allowed) - is the JOIN sweeping channels?")
            if radios:
                problems.append(f"the ?WIFI radio line left the mesh channel {len(radios)} time(s): {radios[:2]}")
        finally:
            if changed:
                _restore_ap(w, tok, ssid, problems)
    bench.note(f"wifi.join_absent_ssid_keeps_mesh: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ the PC on the AP (attended)
def _netsh(*args, timeout=30):
    r = subprocess.run(["netsh", "wlan", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def _wlan_interfaces():
    """Every WiFi adapter as {'name', 'description', 'state', 'ssid', 'profile'} (`netsh wlan show interfaces`)."""
    ifaces, cur = [], None
    for line in _netsh("show", "interfaces").splitlines():
        m = re.match(r"^\s*(Name|Description|State|SSID|Profile)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "name":
            cur = {"name": val}
            ifaces.append(cur)
        elif cur is not None and key not in cur:
            cur[key] = val
    return ifaces


def _iface(name):
    return next((i for i in _wlan_interfaces() if i["name"] == name), None)


def _joined(name, ssid):
    i = _iface(name) or {}
    return i.get("state", "").lower() == "connected" and i.get("ssid") == ssid


def _internet_adapter():
    """The adapter carrying the PC's default route, or None."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | "
                            "Select-Object -First 1).InterfaceAlias"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _pick_adapter(bench):
    """(adapter, why): bench.json "wifi_test_interface" when set; otherwise a WiFi adapter that does not carry the
    PC's default route, so the PC stays online (this bench's TP-Link "Wi-Fi 2"); otherwise the only one, which takes
    the PC offline until the test puts it back."""
    ifaces = _wlan_interfaces()
    want = bench.cfg.get("wifi_test_interface")
    if want:
        return next((i for i in ifaces if i["name"] == want), None), "bench.json wifi_test_interface"
    inet = _internet_adapter()
    spare = [i for i in ifaces if i["name"] != inet]
    if spare:
        return spare[0], f"not the internet adapter ({inet or 'none found'})"
    return (ifaces[0], "the only WiFi adapter: the PC is offline until the test puts it back") if ifaces else (None, "")


def _wait(pred, timeout, step=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _profile_xml(name, ssid, pw):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ('<?xml version="1.0"?>\n<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
            f'  <name>{esc(name)}</name>\n  <SSIDConfig><SSID><name>{esc(ssid)}</name></SSID></SSIDConfig>\n'
            '  <connectionType>ESS</connectionType>\n  <connectionMode>manual</connectionMode>\n'
            '  <MSM><security>\n    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption>'
            '<useOneX>false</useOneX></authEncryption>\n'
            f'    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{esc(pw)}</keyMaterial></sharedKey>\n'
            '  </security></MSM>\n</WLANProfile>\n')


@test("wifi.pc_joins_ap_ws", "(attended) A WiFi adapter on the PC joins W1's access point, opens ws://192.168.4.1/ws and has ?VERSION answered over it while W1 counts the client, then returns to the network it was on; the spare adapter is used when there is one, so the PC stays online", needs=["wcb1"], links=[], opt_in="wifi_pc")
def pc_joins_ap_ws(bench):
    """WCB_WS.cpp: a text frame per line, the console output teed back as text frames. Read-only on the board.

    Windows side: a temporary profile named HIL-<ssid> on the chosen adapter only, never one of the PC's own
    profiles, and every netsh call names the adapter - with two adapters an unnamed `netsh wlan connect` is refused
    (run 20260924-092602 failed that way, and reused the PC's own WCB1 profile on the other adapter). The profile holds
    W1's AP password from its chain and is deleted afterwards; netsh's replies are logged, and they carry no key."""
    if os.name != "nt":
        raise Skip("netsh (Windows) drives the PC's WiFi here")
    w = usb_wcb(bench)
    _, mode, ssid, pw = _wifi_token(bench.config_tokens(1, refresh=True))
    if mode != "AP" or not pw or not ssid:
        raise Skip("W1 does not host a named access point with a password")
    st = _status(w)
    if st.get("Interface") != "up" or not st.get("WS endpoint", "").startswith("ws://"):
        raise Skip(f"W1's access point is not up: Interface {st.get('Interface')!r}, WS {st.get('WS endpoint')!r}")
    ip = st.get("IP address", "")
    adapter, why = _pick_adapter(bench)
    if not adapter:
        raise Skip("this PC has no WiFi adapter" if not why else "bench.json wifi_test_interface names no WiFi adapter here")
    name = adapter["name"]
    prev = adapter.get("profile") if adapter.get("state", "").lower() == "connected" else None
    tmp = f"HIL-{ssid}"
    bench.note(f"adapter {name} ({why}); it was on {adapter.get('ssid') or '(nothing)'}; temporary profile {tmp} for "
               f"{ssid} at {ip}")
    problems = []
    added = False
    fd, path = tempfile.mkstemp(prefix="wlan-", suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_profile_xml(tmp, ssid, pw))
        out = _netsh("add", "profile", f"filename={path}", f"interface={name}", "user=current")
        try:
            os.remove(path)
        except OSError:
            pass
        bench.note("netsh add profile: " + redact_text(" ".join(out.split()))[:200])
        added = "is added" in out
        if not added:
            raise AssertionError("netsh did not add the temporary profile")
        out = _netsh("connect", f"name={tmp}", f"ssid={ssid}", f"interface={name}")
        bench.note("netsh connect: " + " ".join(out.split())[:200])
        if not _wait(lambda: _joined(name, ssid), 30):
            now = _iface(name) or {}
            problems.append(f"{name} did not associate with W1's access point within 30 s "
                            f"(state {now.get('state')!r}, SSID {now.get('ssid')!r})")
        else:
            ws = None
            end = time.monotonic() + 25
            while ws is None and time.monotonic() < end:          # DHCP, then the endpoint
                try:
                    ws = WsClient(ip, timeout=4.0)
                except OSError:
                    time.sleep(1.5)
            if ws is None:
                problems.append(f"could not open ws://{ip}/ws within 25 s of associating")
            else:
                try:
                    ws.send_text("?VERSION\n")
                    text = ws.read_until("End of Version", timeout=6)
                    if "Software Version:" not in text:
                        problems.append(f"?VERSION over the WebSocket was not answered (got {text[:80]!r})")
                    ep = _status(w).get("WS endpoint", "")
                    if not re.search(r"\([1-9]\d* client\(s\) connected\)$", ep):
                        problems.append(f"W1 counts no WebSocket client while one is open: {ep!r}")
                finally:
                    ws.close()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        _netsh("disconnect", f"interface={name}")
        if added:
            bench.note("netsh delete profile: " + " ".join(_netsh("delete", "profile", f"name={tmp}",
                                                                  f"interface={name}").split())[:200])
        if prev:
            _netsh("connect", f"name={prev}", f"interface={name}")
            if not _wait(lambda: (_iface(name) or {}).get("state", "").lower() == "connected", 45):
                problems.append(f"{name} did not reconnect to {prev} within 45 s: reconnect it by hand")
    time.sleep(2)
    ep = _status(w).get("WS endpoint", "")
    if not ep.endswith("(0 client(s) connected)"):
        problems.append(f"after the PC left, W1 still counts a client: {ep!r}")
    assert not problems, "; ".join(problems)
