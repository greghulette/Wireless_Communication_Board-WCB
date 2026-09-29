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
- `wifi_pc` (attended): for each test a WiFi adapter on the PC joins W1's access point (_pc_on_w1_ap), opens
  ws://192.168.4.1/ws, and returns to the network it was on. It uses an adapter that does not carry the PC's internet
  when there is one (this bench's TP-Link "Wi-Fi 2"); with a single adapter the PC is offline for about 30 s a test.
  Over the endpoint (WCB-WP22): ?VERSION answered; line framing, the over-long line and the oversized frame; ?backup
  whole and UTF-8-clean; the three client slots and the eviction of the oldest; a DATA-sized ?OTALOCAL session (with
  ota_erase too); and W1's DHCP handing out no gateway.

Credentials: the AP and JOIN lines carry a password. It is read from the board's own chain at run time and given
back to a board or to a Windows WiFi profile that is removed afterwards; it never goes into a note, a message or a
file that stays. (?backup output, and so the session log, already carries it, as it always has.) Neither does an SSID:
W1's own or derived name is compared, never quoted, and a token is shown only as its hash (redact_token). The setter
tests' throwaway SSIDs and passphrases (HIL + a nonce) are never used for a network. A restore if aborted: replay W1's
own ?WIFI line from its chain on its USB console (and reboot, if the board was rebooted since it changed); on the PC,
`netsh wlan delete profile name=HIL-<W1's SSID> interface=<adapter>` and `netsh wlan connect name=<its network>
interface=<adapter>`.
"""
import contextlib
import os
import re
import time

from hil import optin
from hil.checkpoint import redact_token
from hil.runner import Skip, test
from hil.wcb import WCB
from hil.wlan import default_routes as _default_routes
from hil.wlan import pc_on_ap as _pc_on_ap
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


@test("wifi.join_w2_ap", "W1 joins W2's access point: JOIN saved for the next boot, then W1 associates on the mesh channel (boot lines, the ?WIFI JOIN block with an address and a WS endpoint, W2 counting a client, the WebSocket ready line) and the mesh still delivers; with wifi_pc ticked as well, the PC joins W2's access point too and ?VERSION is answered over W1's endpoint on its joined address; W1's own access point is put back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes")
def join_w2_ap(bench):
    """wcbWifiJoinTry pins the association to meshChannel, and the JOIN state machine verifies the radio stayed there
    (WCB_WiFi.cpp). W2's AP is on the same mesh channel, so the join is expected to settle 'connected'. Once joined,
    wcbWsService starts the endpoint and prints '[WS] command endpoint ready - ws://<ip>/ws' (WCB_WS.cpp, wcbWsBegin),
    the line NaviLink waits for (WCB-WP22 row 4); _pc_reaches_joined_w1 is that row's second half."""
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
            try:                                     # NaviLink waits for this exact line (WCB_WS.cpp, wcbWsBegin)
                w.dev.expect(r"^\[WS\] command endpoint ready — ws://192\.168\.4\.\d+/ws$", timeout=10, since=bm)
            except AssertionError:
                problems.append("no '[WS] command endpoint ready — ws://...' line after joining")
            st = _status(w)
            if st.get("Mode") != "JOIN" or st.get("Join SSID") != ssid2:
                problems.append(f"Mode {st.get('Mode')!r}, Join SSID "     # compared, never quoted
                                f"{'is' if st.get('Join SSID') == ssid2 else 'is not'} W2's access point name")
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
            if "wifi_pc" in optin.enabled(bench.cfg):
                _pc_reaches_joined_w1(bench, problems, ssid2, pw2, st.get("IP address", ""))
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
# The PC's side - netsh, the temporary profile, the lease wait, the routes - is hil/wlan.py (NAVICORE.md INF5), which
# suites/s45_navicore_wifi.py shares.
@contextlib.contextmanager
def _pc_on_w1_ap(bench, problems):
    """The PC on W1's access point for the block (_pc_on_ap) -> (w, W1's address, the adapter's name). Skip unless W1
    hosts a named, password-protected access point that is up with its WebSocket endpoint running."""
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
    with _pc_on_ap(bench, problems, ssid, pw, "W1's") as name:
        yield w, ip, name


def _ws_open(ip, wait=15.0):
    """A WsClient to ws://<ip>/ws, retried for `wait` s (the lease is already held) -> the client, or None."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            return WsClient(ip, timeout=4.0)
        except OSError:
            time.sleep(1.5)
    return None


def _ws_until(ws, pred, timeout):
    """Read frames into ws.text until pred(ws.text) or the time is up -> pred's last value. A socket the board closed
    raises ConnectionError (hil/ws.py)."""
    end = time.monotonic() + timeout
    while not pred(ws.text) and time.monotonic() < end:
        ws.read_until("\x00never\x00", timeout=min(0.4, max(0.05, end - time.monotonic())))
    return pred(ws.text)


def _ws_quiet(ws, quiet=1.5, cap=20.0):
    """Read frames until none has arrived for `quiet` s, or for `cap` s in all."""
    end = time.monotonic() + cap
    size, since = len(ws.text), time.monotonic()
    while time.monotonic() < end and time.monotonic() - since < quiet:
        ws.read_until("\x00never\x00", timeout=0.3)
        if len(ws.text) != size:
            size, since = len(ws.text), time.monotonic()


def _ws_client_count(w):
    m = re.search(r"\((\d+) client\(s\) connected\)$", _status(w).get("WS endpoint", ""))
    return int(m.group(1)) if m else None


def _left_clean(bench, w, problems):
    """After the PC left: W1 counts no WebSocket client. W1's heap is noted, low-water mark included: with the access
    point up a classic ESP32 has about 18 KB, and a PC's association and socket traffic take the low-water mark within
    a few KB of nothing (tracker #111)."""
    time.sleep(2)
    st = _status(w)
    ep = st.get("WS endpoint", "")
    bench.note(f"W1 after the PC left: free heap {st.get('Free heap')}")
    if not ep.endswith("(0 client(s) connected)"):
        problems.append(f"after the PC left, W1 still counts a client: {ep!r}")


@test("wifi.pc_joins_ap_ws", "(attended) A WiFi adapter on the PC joins W1's access point, opens ws://192.168.4.1/ws and has ?VERSION answered over it while W1 counts the client, then returns to the network it was on; the spare adapter is used when there is one, so the PC stays online", needs=["wcb1"], links=[], opt_in="wifi_pc")
def pc_joins_ap_ws(bench):
    """WCB_WS.cpp: a text frame per line, the console output teed back as text frames. Read-only on the board. The
    join and the way back are _pc_on_ap's."""
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        if ws is None:
            problems.append(f"could not open ws://{ip}/ws with a lease held")
        else:
            try:
                ws.send_text("?VERSION\n")
                text = ws.read_until("End of Version", timeout=6)
                if "Software Version:" not in text:
                    problems.append(f"?VERSION over the WebSocket was not answered (got {text[:80]!r})")
                if not (_ws_client_count(w) or 0) >= 1:
                    problems.append("W1 counts no WebSocket client while one is open")
            finally:
                ws.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("ws.line_framing", "OPT-IN (wifi_pc): the WebSocket endpoint's line assembly - CR and LF both end a line and two lines in one frame both run, a line split over two frames runs once, a leading space is trimmed, a line over 1535 characters is dropped whole (neither its head nor its tail runs), and a frame over 3072 bytes closes the socket and is counted", needs=["wcb1"], links=["W1S2"], opt_in="wifi_pc")
def ws_line_framing(bench):
    """WCB-WP22 row 1 (ws.inbound_line_framing_and_overrun). accFeed (WCB_WS.cpp) ends a line at CR or LF and queues
    it; a line reaching WS_LINE_MAX - 1 (1535) characters is dropped to its end, never restarted - a restarted tail has
    no prefix, so it would be a broadcast to every board. wcbWsService trims each line before running it, as the USB
    reader does. wsHandler reads a frame into a 3072-byte buffer; a longer frame fails the request (httpd closes the
    socket) and counts, which loop() reports as '[WS] dropped N inbound command(s) - queue full or oversized'. The
    dropped line is ';S2<marker>' plus filler: W1 S2 would show the marker if the line ran, and W1 S2 or W2 S3 (the
    ports that take broadcasts here, mesh.frag_origin_rebroadcast) the filler if its tail ran as a broadcast."""
    s2 = link(bench, 1, "S2")
    w2s3 = bench.links.get(2, "S3")
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        try:
            ws.text = ""
            ws.send_text("?VERSION\r?VERSION\n")
            if not _ws_until(ws, lambda t: t.count("End of Version") >= 2, 8):
                problems.append(f"two lines in one frame (CR, then LF): {ws.text.count('End of Version')} answered, not 2")
            ws.text = ""
            ws.send_text("?VER")
            time.sleep(0.3)
            ws.send_text("SION\n")
            _ws_until(ws, lambda t: "End of Version" in t, 6)
            _ws_until(ws, lambda t: False, 1.0)                       # a second answer, had the halves run apart
            if ws.text.count("End of Version") != 1:
                problems.append(f"a line split over two frames: {ws.text.count('End of Version')} answers, not 1")
            ws.text = ""
            ws.send_text(" ?VERSION\n")
            if not _ws_until(ws, lambda t: "End of Version" in t, 6):
                problems.append("a line with a leading space was not run")
            watch = Watch(s2, w2s3)
            mk = marker("L")
            ws.text = ""
            ws.send_text(f";S2{mk}" + "Q" * 1600 + "\n")
            time.sleep(1.5)
            ws.send_text("?VERSION\n")
            if not _ws_until(ws, lambda t: "End of Version" in t, 6):
                problems.append("after an over-long line the next line did not run")
            if mk.encode() in watch.got(s2):
                problems.append("the over-long line ran (its ;S2 marker reached W1 S2)")
            for l in (s2, w2s3):
                if l is not None and b"QQQQ" in watch.got(l):
                    problems.append(f"the over-long line's tail ran as a broadcast (its filler reached {l.key})")
            wm = w.dev.mark()
            closed = False
            try:
                ws.send_text("?VERSION" + " " * 3100 + "\n")
                ws.read_until("\x00never\x00", timeout=3)
                ws.text = ""
                ws.send_text("?VERSION\n")
                closed = "End of Version" not in ws.read_until("End of Version", timeout=3)
            except (ConnectionError, OSError):
                closed = True
            if not closed:
                problems.append("a 3109-byte frame did not close the socket (a later ?VERSION was still answered)")
            try:
                w.dev.expect(r"^\[WS\] dropped \d+ inbound command\(s\)", timeout=4, since=wm)
            except AssertionError:
                problems.append("W1 did not report the oversized frame ('[WS] dropped N inbound command(s)')")
        finally:
            ws.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("ws.backup_over_ws", "OPT-IN (wifi_pc): bulk output over the WebSocket arrives whole - ?backup's 'For Configured Boards' chain passes its CRC and equals the one USB prints, every text frame is valid UTF-8 on its own, and ?HELP sent over the socket comes back as every line W1's USB console printed, in order", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ws_backup_over_ws(bench):
    """WCB-WP22 row 2 (ws.outbound_tee_integrity). The tee (wcbWsSinkWrite, WCB_WS.cpp) copies console output into a
    2 KB sink and flushes it inline when full on the loop task; sinkPump cuts each send back to a whole UTF-8
    character, so a bulk reply goes out in several frames and each must decode on its own. The console and the socket
    carry the same bytes, so every line USB shows during ?HELP must be in the socket's text in the same order (a line
    the tee dropped is reported on both as '[WS] dropped N output line(s)', quoted if seen). ?backup's chains carry
    W1's credentials: only their CRC checks and equality are compared, never shown."""
    from suites.s05_mesh import _chain_ok, _live_chain
    problems = []
    usb_chain = _live_chain([x.rstrip() for x in usb_wcb(bench).run("?backup", timeout=15)], False)
    if not _chain_ok(usb_chain):
        raise Skip("W1's own ?backup chain fails its CRC on USB (another task's line inside it?): nothing to compare")
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        try:
            ws.text, ws.text_frames = "", []
            ws.send_text("?backup\n")
            if not _ws_until(ws, lambda t: "End of Backup" in t, 20):
                problems.append(f"?backup over the WebSocket did not finish ({len(ws.text)} characters in 20 s)")
            chain = _live_chain([x.rstrip() for x in ws.text.split("\n")], False)
            if not _chain_ok(chain):
                problems.append(f"the WebSocket's chain ({len(chain or '')} characters) fails its CRC")
            elif chain != usb_chain:
                problems.append(f"the WebSocket's chain differs from USB's ({len(chain)} vs {len(usb_chain)} characters)")
            backup_frames = len(ws.text_frames)
            m = w.dev.mark()
            ws.text = ""
            ws.send_text("?HELP\n")
            _ws_quiet(ws)
            usb = [x.rstrip() for x in w.dev.since(m) if x.strip()]
            _ws_quiet(ws, quiet=0.8, cap=3)                           # anything the socket still owed
            got = [x.rstrip() for x in ws.text.split("\n") if x.strip()]
            k, missing = 0, None
            for x in usb:
                while k < len(got) and got[k] != x:
                    k += 1
                if k == len(got):
                    missing = x
                    break
                k += 1
            if missing is not None:
                drops = [x for x in usb if x.startswith("[WS] dropped")]
                problems.append(f"?HELP: the socket lacks a console line ({missing[:70]!r}; {len(usb)} console lines, "
                                f"{len(got)} socket lines{'; W1 said ' + drops[0] if drops else ''})")
            bad = 0
            for f in ws.text_frames:
                try:
                    f.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    bad += 1
            if bad:
                problems.append(f"{bad} of {len(ws.text_frames)} text frame(s) are not valid UTF-8 on their own")
            bench.note(f"?backup over WS: {backup_frames} frame(s), chain {len(chain or '')} characters; ?HELP: "
                       f"{len(usb)} console lines, {len(got)} socket lines, {len(ws.text_frames) - backup_frames} frame(s)")
        finally:
            ws.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("ws.client_slots", "OPT-IN (wifi_pc): at most three WebSocket clients - a fourth evicts the least recently used, whose socket closes - each keeps its own line buffer, so a line split over frames on one client never fuses with another's, and a broadcast typed over the socket right after W2 sent W1 a command still reaches the mesh", needs=["wcb1", "wcb2"], links=["W1S2", "W2S3"], opt_in="wifi_pc")
def ws_client_slots(bench):
    """WCB-WP22 row 3 (ws.clients_slots_and_source_flags). wcbWsBegin sets max_open_sockets to WS_MAX_CLIENTS (3) with
    lru_purge_enable, so httpd closes the least recently used session to take a fourth; accFor keeps one accumulator
    per socket (WCB_WS.cpp), so interleaved partial lines from two clients stay apart. sinkPump sends the console to
    every client, so each open client sees every answer: two commands, two answers on each. The source flags:
    wcbWsService clears lastReceivedViaESPNOW and inSequenceBody before it runs a socket's line, as the USB reader
    does, so a broadcast typed there right after a mesh-received command (;W1;S2 from W2's console) is not taken for a
    received one and dropped from the mesh: it reaches W2 S3, the port that takes broadcasts on W2."""
    s2, w2s3 = link(bench, 1, "S2"), link(bench, 2, "S3")
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        clients = []
        try:
            for k in range(3):
                c = _ws_open(ip)
                assert c is not None, f"client {k + 1} could not open ws://{ip}/ws with a lease held"
                clients.append(c)
                c.send_text("?VERSION\n")
                c.read_until("End of Version", timeout=6)
            n = _ws_client_count(w)
            if n != 3:
                problems.append(f"with three clients open W1 counts {n}")
            fourth = _ws_open(ip, wait=8)
            assert fourth is not None, "a fourth client could not connect at all"
            clients.append(fourth)
            time.sleep(1.0)
            first = clients[0]
            try:
                _ws_quiet(first, quiet=0.8, cap=4)      # what the others' answers left in its socket
                alive = marker("E")                      # ;S0 prints it on the console, which the tee sends to all
                first.text = ""
                first.send_text(f";S0,{alive}\n")
                evicted = alive not in first.read_until(alive, timeout=3)
            except (ConnectionError, OSError):
                evicted = True
            if not evicted:
                problems.append("the first client still answers after a fourth connected: nothing was evicted")
            n = _ws_client_count(w)
            if n != 3:
                problems.append(f"after the fourth connected W1 counts {n}, not 3")
            a, b = clients[1], clients[2]
            _ws_until(a, lambda t: False, 0.5)
            _ws_until(b, lambda t: False, 0.5)
            a.text, b.text = "", ""
            a.send_text("?VER")
            b.send_text("?VERSION\n")
            time.sleep(0.3)
            a.send_text("SION\n")
            for c, who in ((a, "second"), (b, "third")):
                _ws_until(c, lambda t: t.count("End of Version") >= 2, 6)
                _ws_until(c, lambda t: False, 0.5)
                if c.text.count("End of Version") != 2:
                    problems.append(f"interleaved partial lines: the {who} client saw "
                                    f"{c.text.count('End of Version')} answers, not 2")
            with Console(bench, 2) as c2:
                via, bc = marker("V"), marker("B")
                watch = Watch(s2, w2s3)
                c2.send(f";W1;S2{via}")
                try:
                    watch.expect(s2, via.encode(), timeout=4)
                except AssertionError:
                    problems.append("W2's ;W1;S2 never reached W1 S2: the source-flag arm has no mesh command before it")
                a.send_text(f"{bc}\n")
                try:
                    watch.expect(w2s3, bc.encode(), timeout=5)
                except AssertionError:
                    problems.append("a broadcast typed over the socket after a mesh-received command never reached W2 S3")
        finally:
            for c in clients:
                c.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("wifi.ap_dhcp_no_gateway", "OPT-IN (wifi_pc): W1's access point hands the PC an address with no gateway, so the PC's default route stays where it was", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ap_dhcp_no_gateway(bench):
    """WCB-WP22 row 6 (wifi.ap_dhcp_no_gateway). wcbWifiStartAP (WCB_WiFi.cpp) clears the DHCP server's router option,
    so a PC on the droid's access point keeps its real internet route. On the adapter the test joined, which holds a
    192.168.4.x lease by then (_pc_on_ap): no default route, and every other adapter's default routes the same as
    before the join."""
    problems = []
    before_all = _default_routes()
    with _pc_on_w1_ap(bench, problems) as (w, ip, name):
        time.sleep(2.0)                                     # the lease's routes settle
        during = _default_routes()
        if during is None or before_all is None:
            problems.append("Get-NetRoute could not be read")
        else:
            mine = [r for r in during if r.startswith(name + "|")]
            if mine:
                problems.append(f"W1's DHCP gave the adapter a default route: {mine}")
            others = [r for r in during if not r.startswith(name + "|")]
            was = [r for r in before_all if not r.startswith(name + "|")]
            if others != was:
                problems.append(f"the other adapters' default routes changed while on W1's access point: {was} -> {others}")
        bench.note(f"default routes on {name} while on W1's access point: "
                   f"{[r for r in (during or []) if r.startswith(name + '|')] or 'none'}")
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("ws.ota_chunk", "OPT-IN (wifi_pc, and ota_erase for the session): ?OTALOCAL lines work over the WebSocket - a 4096-byte BEGIN, one 1024-byte DATA line (about 1.4 KB, inside the 1535-character line limit) ACKed as [OTA:ACK,1024], then ABORT leaves STATUS idle", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ws_ota_chunk(bench):
    """WCB-WP22 row 5 (ota.over_websocket). WS_LINE_MAX (WCB_WS.cpp) is sized for a DATA line: base64 of a 1 KB chunk
    plus its prefix is about 1,390 characters, and processOtaLocalCommand (WCB_OTA.cpp) ACKs it by offset. The chunk is
    the head of the image W1 runs (s20's _image), as ota.local_begin_supersede sends over USB. BEGIN erases the head of
    W1's inactive slot, so this also needs ota_erase; ABORT closes the session with no boot switch, and s20's _local
    puts W1 back to idle over USB whatever happens."""
    from suites.s20_ota import _b64, _image, _local
    if "ota_erase" not in optin.enabled(bench.cfg):
        raise Skip('opt-in: add "ota_erase" to bench.json "opt_in" as well (a BEGIN erases the head of the inactive '
                   'app slot)')
    img = _image(usb_wcb(bench))
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        status = ""
        with _local(w):
            try:
                ota = lambda: [x.strip() for x in ws.text.splitlines() if x.strip().startswith("[OTA")]
                ws.text = ""
                ws.send_text("?OTALOCAL,BEGIN,4096,0\n")
                _ws_until(ws, lambda t: "[OTA:BEGIN," in t, 15)
                if "[OTA:BEGIN,OK,0]" not in ota():
                    problems.append(f"BEGIN over the WebSocket: {ota()[:3]}")
                else:
                    line = f"?OTALOCAL,DATA,0,{_b64(img[0:1024])}"
                    ws.text = ""
                    ws.send_text(line + "\n")
                    _ws_until(ws, lambda t: "[OTA:ACK," in t or "[OTA:NAK," in t, 10)
                    if "[OTA:ACK,1024]" not in ota():
                        problems.append(f"a {len(line)}-character DATA line: {ota()[:3]}")
                ws.text = ""
                ws.send_text("?OTALOCAL,ABORT\n")
                time.sleep(1.0)
                ws.send_text("?OTALOCAL,STATUS\n")
                _ws_until(ws, lambda t: "Session:" in t, 6)
                status = next((x.strip() for x in ws.text.splitlines() if x.startswith("Session:")), "")
            finally:
                ws.close()
        if status != "Session:     idle":
            problems.append(f"after ABORT over the WebSocket, STATUS reads {status!r}, not 'Session:     idle'")
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


def _pc_reaches_joined_w1(bench, problems, ssid2, pw2, ip1):
    """WCB-WP22 row 4's second half, run by wifi.join_w2_ap when wifi_pc is ticked as well: the PC joins W2's access
    point too and opens ws://<W1's joined address>/ws, where ?VERSION is answered - W1's endpoint serves on a JOIN
    address, reached through W2's access point. A Skip here (not Windows, no adapter) is noted, not raised: the JOIN
    half has run by then."""
    try:
        with _pc_on_ap(bench, problems, ssid2, pw2, "W2's"):
            ws = _ws_open(ip1)
            if ws is None:
                problems.append(f"the PC on W2's access point could not open ws://{ip1}/ws, W1's joined address")
                return
            try:
                ws.send_text("?VERSION\n")
                if "End of Version" not in ws.read_until("End of Version", timeout=6):
                    problems.append("?VERSION over W1's endpoint on its joined address was not answered")
            finally:
                ws.close()
    except Skip as e:
        bench.note(f"the PC half is skipped: {e}")
