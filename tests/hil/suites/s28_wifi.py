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
- The WebSocket token (docs/WIFI_DESIGN.md §7): the ?WIFI,TOKEN setter with no opt-in (no reboot: a token applies at
  once, to sessions opened after it); under wifi_modes a JOIN with no token refused at boot; under wifi_pc a throwaway
  token on W1 and the sign-in seen from the socket - signed out, a wrong token, the right one. JOIN needs a token, so
  every JOIN test gives W1 a throwaway one first (_token_for_join) and takes it away after, unless W1 has its own. Every
  client these tests open to W1 signs in with W1's own token when W1 asks for one (_ws_open; hil/ws.py), so a bench W1
  with a token runs them unchanged.

Credentials: the AP and JOIN lines carry a password. It is read from the board's own chain at run time and given
back to a board or to a Windows WiFi profile that is removed afterwards; it never goes into a note, a message or a
file that stays. (?backup output, and so the session log, already carries it, as it always has.) Neither does an SSID:
W1's own or derived name is compared, never quoted, and a token is shown only as its hash (redact_token). The setter
tests' throwaway SSIDs and passphrases (HIL + a nonce) are never used for a network. A WebSocket token is never quoted
either: W1's own is read from its chain and handed back to it or to a client. A restore if aborted: replay W1's own
?WIFI line from its chain on its USB console (and reboot, if the board was rebooted since it changed), and its own
?WIFI,TOKEN line, or ?WIFI,TOKEN,CLEAR when its chain had none; on the PC, `netsh wlan delete profile
name=HIL-<W1's SSID> interface=<adapter>` and `netsh wlan connect name=<its network> interface=<adapter>`.
"""
import contextlib
import os
import re
import struct
import time

from hil import optin
from hil.checkpoint import redact_token
from hil.runner import Skip, test
from hil.wcb import WCB
from hil.wlan import default_routes as _default_routes
from hil.wlan import pc_on_ap as _pc_on_ap
from hil.wlan import realtek_enabled as _realtek_enabled
from hil.ws import AUTH_OK, AUTH_REQUIRED, CLOSE_POLICY, AuthFailed, WsClient
from hil.ws import frame as ws_frame
from suites.common import (Console, Watch, config_guard, fault, fault_off, link, marker, nonce, snapshot, token,
                           usb_wcb, wifi_mode_token, ws_token)
from suites.s03_wcb import _w2_online
from suites.s45_navicore_wifi import navicore_home as _navicore_home
from suites.s18_etm_config_wdp import HOLD_BAUD, _hold_line


def _has(lines, text):
    return any(text in x for x in lines)


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _wifi_token(tokens):
    """(the whole ?WIFI mode token, MODE, ssid, password) from a chain, passing over ?WIFI,TOKEN (wifi_mode_token). The
    password is only ever handed back to a board."""
    t = wifi_mode_token(tokens)
    if t is None:
        raise Skip("the chain lacks a ?WIFI line")
    parts = t.split(",")
    mode = parts[1].upper()
    ssid = parts[2] if len(parts) > 2 else ""
    pw = ",".join(parts[3:]) if len(parts) > 3 else ""
    return t, mode, ssid, pw


def _status(w, timeout=5.0):
    """The ?WIFI block as {label: value} ('Mode', 'Interface', 'IP address', 'Radio channel', 'WS endpoint', ...)."""
    out = {}
    for x in w.run("?WIFI", timeout=timeout):
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
def _net80211(w):
    """The entries ?NVS counts for the WiFi driver's own namespace, nvs.net80211 (0 when it has none). setup() calls
    WiFi.persistent(false) (WCB.ino) so no WiFi.begin / softAP / mode change writes it; with persistence on, a JOIN's
    begin stores the station config there (a JOIN retrying every 5 s once grew it from 36 to 95 entries; coverage
    re-scan 2026-10-12, boot.wifi_persistent_off)."""
    for x in w.run("?NVS", timeout=6):
        m = re.match(r"^  nvs\.net80211\s+(\d+)$", x.rstrip())
        if m:
            return int(m.group(1))
    return 0


def _derived_ssid(tokens, n):
    """The name wcbWifiDefaultSsid() builds from the chain: 'WCB-<alias>', or 'WCB-<n>' with no alias, cut to 32
    characters (WCB_WiFi.cpp:71-79). An SSID: compared, never printed."""
    a = token(tokens, "?ALIAS,")
    return ("WCB-" + (a.split(",", 1)[1] if a else str(n)))[:32]


def _wifi_now(bench):
    """W1's ?WIFI mode token as its chain holds it now, or None."""
    return wifi_mode_token(snapshot(bench, 1))


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
        ("an unknown verb", "?WIFI,BOGUS", ["Usage: ?WIFI | ?WIFI,OFF | ?WIFI,AP,<ssid>,<pass> | ?WIFI,JOIN,<ssid>,<pass>",
                                            "       ?WIFI,TOKEN,<token> | ?WIFI,TOKEN,CLEAR"]),
    )
    problems = []
    with config_guard(bench, 1) as before:
        tok = wifi_mode_token(before[1])
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
        tok = wifi_mode_token(before[1])
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
        tok = wifi_mode_token(before[1])
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


# ============================================================ the WebSocket token (docs/WIFI_DESIGN.md §7)
TOKEN_SET = "WebSocket token set. Sessions opened from now on must send ?AUTH,<token>"
TOKEN_CLEARED = "WebSocket token cleared. Sessions opened from now on need no sign-in."
JOIN_WARNING = "WARNING: no WebSocket token is set, and JOIN will not start at the next boot"
JOIN_REFUSED = "[WIFI] JOIN REFUSED: no WebSocket token is set, so WiFi stays OFF."


def _throwaway_token():
    """A token for one test: 12 characters, none of those the grammar reserves (wcbWifiTokenProblem, WCB_WiFi.cpp)."""
    return f"hil{nonce().lower()}tok"


def _token_set(w, t, what="setup"):
    if not _has(w.run(f"?WIFI,TOKEN,{t}"), TOKEN_SET):
        raise AssertionError(f"{what}: ?WIFI,TOKEN did not confirm")


def _token_put_back(w, before, problems):
    """W1's own token state from its chain `before`: its own token replayed (never quoted), or none. A token applies at
    once (wcbWsSetToken), so nothing reboots."""
    own = ws_token(before)
    ok = _has(w.run(f"?WIFI,TOKEN,{own}"), TOKEN_SET) if own else _has(w.run("?WIFI,TOKEN,CLEAR"), TOKEN_CLEARED)
    if not ok:
        problems.append("W1's own WebSocket token did not go back: replay its ?WIFI,TOKEN line from its ?backup on its "
                        "USB console, or ?WIFI,TOKEN,CLEAR if it had none")


def _token_for_join(w, before):
    """A WebSocket token on W1 before it is given a JOIN, which does not start without one (wcbWifiStart,
    WCB_WiFi.cpp) -> the token in force: W1's own when its chain has one, else a throwaway one that _token_put_back
    takes away again."""
    own = ws_token(before)
    if own:
        return own
    t = _throwaway_token()
    _token_set(w, t)
    return t


@test("wifi.token_setter", "?WIFI,TOKEN refuses a token under 8 or over 32 characters, or holding a space or any of , ; ? \" \\, and saves nothing; a good one is saved ('WebSocket token set'), ?WIFI's Token line says 'set' and never the token, and the chain carries ?WIFI,TOKEN,<token> right ahead of the ?WIFI mode line; ?WIFI,TOKEN,CLEAR takes it out ('none'); a ?WIFI,JOIN saved with no token warns that JOIN will not start; ?AUTH typed on USB is a no-op; W1's own ?WIFI lines go back (no opt-in: nothing applies before a reboot)", needs=["wcb1"], links=[])
def token_setter(bench):
    """docs/WIFI_DESIGN.md §7. wcbWifiTokenCommand (WCB_WiFi.cpp) checks a token (wcbWifiTokenProblem) before it saves
    it, in the NVS key wifi_cfg/token alone; collectConfigCommands (WCB.ino) emits WIFI,TOKEN just ahead of the mode
    line, so a restore has it before ?WIFI,JOIN. processWifiCommand saves a JOIN with no token and warns: the boot is
    what refuses (wifi.join_needs_token). ?AUTH reaches the dispatcher only from USB or the mesh, where it does nothing
    (WCB.ino, ahead of ?DEBUG). No '^' case: the line reader splits a chain there, so the rest would run as a command of
    its own - a token can never hold one, and the setter refuses it anyway. The tokens, SSID and passphrase are
    throwaway; W1's own ?WIFI line and token are replayed in the finally and never quoted."""
    w = usb_wcb(bench)
    good = _throwaway_token()
    bad = (("7 characters", "hil1234"), ("33 characters", "h" * 33), ("a space", "hil 12345"),
           ("a comma", "hil,12345"), ("a ;", "hil;12345"), ("a ?", "hil?12345"),
           ('a "', 'hil"12345'), ("a \\", "hil\\12345"))
    problems = []
    with config_guard(bench, 1) as before:
        tok = wifi_mode_token(before[1])
        if tok is None:
            raise Skip("the chain lacks a ?WIFI line")
        own = ws_token(before[1])
        changed = False
        try:
            for what, t in bad:
                out = [x.rstrip() for x in w.run(f"?WIFI,TOKEN,{t}")]
                if not any(x.startswith("Token refused: a token ") for x in out):
                    problems.append(f"a token with {what} was not refused")
                if _has(out, TOKEN_SET):
                    problems.append(f"a token with {what} was saved")
            if ws_token(snapshot(bench, 1)) != own:
                problems.append("a refused ?WIFI,TOKEN changed the chain's token")
            changed = True
            _token_set(w, good, "a good token")
            st = _status(w)
            if st.get("Token") != "set":
                problems.append(f"with a token set ?WIFI's Token line is {st.get('Token')!r}, not 'set'")
            if any(good in x for x in w.run("?WIFI")):
                problems.append("?WIFI printed the token")
            toks = snapshot(bench, 1)
            if ws_token(toks) != good:
                problems.append("the chain does not carry the token just set")
            else:
                i = next(k for k, x in enumerate(toks) if x.upper().startswith("?WIFI,TOKEN,"))
                if i + 1 >= len(toks) or toks[i + 1] != wifi_mode_token(toks):
                    problems.append("?WIFI,TOKEN is not right ahead of the ?WIFI mode line in the chain")
            out = [x.rstrip() for x in w.run(f"?AUTH,{good}")]
            if any("Unknown command" in x or "[AUTH" in x for x in out):
                problems.append("?AUTH typed on USB was not a no-op")
            if not _has(w.run("?WIFI,TOKEN,CLEAR"), TOKEN_CLEARED):
                problems.append("?WIFI,TOKEN,CLEAR did not confirm")
            if not _status(w).get("Token", "").startswith("none"):
                problems.append("after ?WIFI,TOKEN,CLEAR ?WIFI's Token line does not say 'none'")
            if ws_token(snapshot(bench, 1)) is not None:
                problems.append("after ?WIFI,TOKEN,CLEAR the chain still carries a token")
            out = [x.rstrip() for x in w.run(f"?WIFI,JOIN,HIL{nonce()},hil{nonce().lower()}pw")]
            if not _has(out, "WiFi mode set to JOIN"):
                problems.append("a throwaway ?WIFI,JOIN was not saved")
            elif not _has(out, JOIN_WARNING):
                problems.append("a ?WIFI,JOIN saved with no token did not warn that JOIN will not start")
            if not _status(w).get("Token", "").startswith("none - JOIN will not start"):
                problems.append(f"in JOIN with no token ?WIFI's Token line is {_status(w).get('Token')!r}")
        finally:
            if changed:
                _put_back(w, bench, tok, problems)
                _token_put_back(w, before[1], problems)
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


@test("wifi.join_w2_ap", "W1 joins W2's access point: JOIN saved for the next boot, then W1 associates on the mesh channel (boot lines, the ?WIFI JOIN block with an address and a WS endpoint, W2 counting a client, the WebSocket ready line) and the mesh still delivers; W1's nvs.net80211 entry count is the same joined and back on its own access point (WiFi.persistent(false)); with wifi_pc ticked as well, the PC joins W2's access point too and ?VERSION is answered over W1's endpoint on its joined address; W1's own access point is put back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes")
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
        net0 = _net80211(w)
        wtok = _token_for_join(w, before[1])        # JOIN does not start without one
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
                _pc_reaches_joined_w1(bench, problems, ssid2, pw2, st.get("IP address", ""), wtok)
            _unicast_ok(w, w2s2, problems, "while joined to W2's AP")
            net1 = _net80211(w)
            if net1 != net0:
                problems.append(f"joining W2's access point changed W1's nvs.net80211 from {net0} to {net1} entries: "
                                f"the WiFi driver is persisting its config")
        finally:
            if changed:
                _restore_ap(w, tok1, ssid1, problems)
            _token_put_back(w, before[1], problems)
        if _status(w).get("Mode") != "AP":
            problems.append("W1 is not back in AP mode")
        net2 = _net80211(w)
        if net2 != net0:
            problems.append(f"after its own access point came back W1's nvs.net80211 holds {net2} entries, {net0} before")
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
        _token_for_join(w, before[1])               # JOIN does not start without one
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
            _token_put_back(w, before[1], problems)
        if _status(w).get("Mode") != "AP":
            problems.append("W1 is not back in AP mode")
    bench.note(f"wifi.join_lost_and_rejoin: {facts}")
    assert not problems, "; ".join(problems)


RETRY_ALLOWANCE = 2     # ETM retries a pinned-channel JOIN may cost in 70 s: radio noise, not a channel sweep


@test("wifi.join_absent_ssid_keeps_mesh", "With channel sync off, a JOIN to a network nobody hosts retries for ever on the mesh channel without sweeping: 'will join', 'still looking (attempt 1)', and '(attempt 12)' about 55 s later, while a unicast to W2 every 5 s for 70 s arrives with no failure and at most two retries and the ?WIFI radio line stays on the mesh channel; W1's own access point and ?CHSYNC,ON are put back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes", opt_in_why="points W1's JOIN at a network nobody hosts for about 70 s and reboots W1 twice")
def join_absent_ssid_keeps_mesh(bench):
    """WCB-WP45 row 2 (wifi.join_absent_ssid_keeps_mesh). wcbWifiJoinTry passes meshChannel to WiFi.begin and keeps
    auto-reconnect off, so the probe for the SSID stays on the channel the mesh already uses (WCB_WiFi.cpp:191-202),
    and the state machine retries every JOIN_RETRY_MS (5 s, :36) for ever, saying so at attempt 1 and every 12th
    (:279-287). A channel sweep would take the radio off the mesh for most of a second every 5 s, and W1's unicasts
    would need retries or fail. The SSID and passphrase are throwaway (HIL + a nonce, never a network); W1's own ?WIFI
    line is replayed at the end. Channel sync is off for the test: with it on, a JOIN board that cannot hear its network
    also looks on every channel on a schedule (at once, then 1, 2, 4... minutes; WCB_WiFi.cpp csFullScanDue,
    docs/CHANNEL_SYNC.md §5), about 1.5 s off the mesh each - deliberate, and not the per-attempt sweep this guards."""
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
        changed = sync_off = False
        _token_for_join(w, before[1])               # JOIN does not start without one
        try:
            if not _has(w.run("?CHSYNC,OFF"), "[CHSYNC] channel sync OFF"):
                problems.append("?CHSYNC,OFF did not confirm")
            sync_off = True
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
            if sync_off:
                w.run("?CHSYNC,ON")
            if changed:
                _restore_ap(w, tok, ssid, problems)
            _token_put_back(w, before[1], problems)
    bench.note(f"wifi.join_absent_ssid_keeps_mesh: {facts}")
    assert not problems, "; ".join(problems)


@test("wifi.join_fault_arms", "W1 joined to W2's access point takes the JOIN state machine's two fault arms: an association that gets no address for 20 s (?DEBUG,FAULT,JOINNOIP) is dropped with 'associated with ... but no address after 20 s — starting over' and the join comes back by itself ('joined ... after N attempt(s)'); an AP read off the mesh channel (?DEBUG,FAULT,JOINCHAN) is hung up on - 'joined ... but its AP is on channel x, not y — that is OFF-MESH. Disconnecting.' - then with channel sync on it is looked for again ('Looking for it on every channel') and joined again where it really is, and with channel sync off the join settles: Association not connected, the radio still on the mesh channel, no 'still looking' for 15 s, and a unicast to W2 still delivered; W1's own access point and ?CHSYNC,ON are put back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes", opt_in_why="joins W1 to W2's access point and reboots W1 twice")
def join_fault_arms(bench):
    """Coverage re-scan 2026-10-12, wifi.join_state_machine (the off-mesh guard and the no-address arm). wcbWifiService
    (WCB_WiFi.cpp) reads the associated AP's own channel (esp_wifi_sta_get_ap_info) and, off the mesh channel,
    disconnects - WiFi.disconnect(), never (true), which would stop WiFi and ESP-NOW with it. With channel sync on (the
    default) it then looks for the network again, on every channel if it has to, and takes the mesh to it
    (docs/CHANNEL_SYNC.md §5); with it off it settles for good (joinSettled): retrying would thrash, and an operator has
    to fix one end. An association with no address for JOIN_ADDR_MS (20 s) is dropped and the join starts over. W2's AP
    is on the mesh channel and hands out addresses, so the JOINCHAN knob (WCB_Fault.h) makes the next check read the AP
    one channel off - once, so with channel sync on the mesh-channel scan finds it again where it is - and JOINNOIP hides
    the address until its arm has fired (a joined board drops its association first, wcbWifiFaultRejoin). The SSID and
    the passphrase are compared, never printed."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
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
        q = re.escape(ssid2)
        joined_rx = rf'^\[WIFI\] joined "{q}" on channel {ch} after \d+ attempt\(s\) — ws://\S+/ws$'
        offmesh_rx = rf'^\[WIFI\] joined "{q}" but its AP is on channel (\d+), not {ch} — that is OFF-MESH\. Disconnecting\.$'
        changed = sync_off = False
        _token_for_join(w, before[1])               # JOIN does not start without one
        try:
            if not _has(w.run(f"?WIFI,JOIN,{ssid2},{pw2}"), f'WiFi mode set to JOIN — joining "{ssid2}"'):
                problems.append("?WIFI,JOIN did not confirm")
            changed = True
            bm = w.reboot()
            try:
                w.dev.expect(joined_rx, timeout=45, since=bm)
            except AssertionError:
                raise AssertionError("W1 did not join W2's access point within 45 s of booting") from None
            # the no-address arm
            fm = w.dev.mark()
            fault(w, "JOINNOIP")
            try:
                w.dev.expect(rf'^\[WIFI\] associated with "{q}" but no address after 20 s — starting over$', timeout=45,
                             since=fm)
                am = w.dev.mark()
                try:
                    w.dev.expect(joined_rx, timeout=30, since=am)
                except AssertionError:
                    problems.append("after 'starting over' W1 did not join W2's access point again within 30 s")
            except AssertionError:
                problems.append("no 'associated ... but no address after 20 s — starting over' line within 45 s")
            # the off-mesh guard, channel sync on (the default): hung up on, then looked for again - and found on the
            # mesh channel, where it is (the knob read it off once)
            om = w.dev.mark()
            fault(w, "JOINCHAN")
            try:
                g = w.dev.expect(offmesh_rx, timeout=10, since=om)
                if g.group(1) == ch:
                    problems.append("the OFF-MESH line names the mesh channel as the AP's")
            except AssertionError:
                problems.append("no OFF-MESH line after JOINCHAN")
            try:
                w.dev.expect(r'^\[WIFI\] Looking for it on every channel - channel sync moves the mesh to it\.$',
                             timeout=5, since=om)
                w.dev.expect(joined_rx, timeout=45, since=om)
            except AssertionError:
                problems.append("with channel sync on, W1 did not look for W2's access point again and rejoin it "
                                "within 45 s of the OFF-MESH hang-up")
            # ...and with channel sync off it settles, as it always did
            if not _has(w.run("?CHSYNC,OFF"), "[CHSYNC] channel sync OFF"):
                problems.append("?CHSYNC,OFF did not confirm")
            sync_off = True
            om = w.dev.mark()
            fault(w, "JOINCHAN")
            try:
                g = w.dev.expect(offmesh_rx, timeout=10, since=om)
                if g.group(1) == ch:
                    problems.append("the OFF-MESH line names the mesh channel as the AP's")
            except AssertionError:
                problems.append("no OFF-MESH line after JOINCHAN with channel sync off")
            time.sleep(15)
            later = [x.rstrip() for x in w.dev.since(om)]
            if _has(later, "[WIFI] still looking for"):
                problems.append("the join kept retrying after the OFF-MESH hang-up")
            if any(re.match(joined_rx, x) for x in later):
                problems.append("W1 joined again after the OFF-MESH hang-up")
            st = _status(w)
            if st.get("Association", "").startswith("connected ("):
                problems.append(f"after the OFF-MESH hang-up Association is {st.get('Association')!r}")
            radio = st.get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"after the OFF-MESH hang-up the radio line is {radio!r}")
            _unicast_ok(w, w2s2, problems, "after the OFF-MESH hang-up")
        finally:
            fault_off(w)
            if sync_off:
                w.run("?CHSYNC,ON")
            if changed:
                _restore_ap(w, tok1, ssid1, problems)
            _token_put_back(w, before[1], problems)
        if _status(w).get("Mode") != "AP":
            problems.append("W1 is not back in AP mode")
    assert not problems, "; ".join(problems)


@test("wifi.join_needs_token", "A JOIN saved with no WebSocket token warns, and the next boot refuses it: 'JOIN REFUSED: no WebSocket token is set, so WiFi stays OFF.', that 7.0.1 joined without one, and the fix (?WIFI,TOKEN,<token>); no 'will join' and no 'still looking' line; ?WIFI shows Mode JOIN, the interface down, no WS endpoint and 'Token : none - JOIN will not start', with the radio on the mesh channel, and the mesh still delivers; W1's own access point and token come back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes", opt_in_why="saves a JOIN with no token on W1 and reboots it twice; W1's WiFi is off in between")
def join_needs_token(bench):
    """docs/WIFI_DESIGN.md §7. wcbWifiStart (WCB_WiFi.cpp) refuses a JOIN with no token at every boot - a board updated
    from 7.0.1 in JOIN mode lands here - and settles the JOIN state machine as a missing SSID does (joinSettled), so the
    radio stays a plain ESP-NOW station on the mesh channel and the WebSocket endpoint never starts. The SSID and
    passphrase are throwaway (never a network); W1's own token, if it has one, is cleared for the boot and replayed
    after, never quoted."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        tok, mode, ssid, _ = _wifi_token(before[1])
        if mode != "AP":
            raise Skip("W1 does not host an access point")
        ch = _mesh_channel(before[1])
        nosuch, pw_x = f"HIL-NOSUCH-{nonce()}", f"hil{nonce().lower()}pass"
        changed = False
        try:
            changed = True
            if ws_token(before[1]) and not _has(w.run("?WIFI,TOKEN,CLEAR"), TOKEN_CLEARED):
                problems.append("?WIFI,TOKEN,CLEAR did not confirm")
            out = w.run(f"?WIFI,JOIN,{nosuch},{pw_x}")
            if not _has(out, f'WiFi mode set to JOIN — joining "{nosuch}"'):
                problems.append("?WIFI,JOIN did not confirm")
            if not _has(out, JOIN_WARNING):
                problems.append("?WIFI,JOIN with no token did not warn")
            bm = w.reboot()
            try:
                w.dev.expect(rf"^{re.escape(JOIN_REFUSED)}$", timeout=15, since=bm)
            except AssertionError:
                problems.append("no 'JOIN REFUSED: no WebSocket token is set' line at boot")
            time.sleep(8)                            # a JOIN would have said 'still looking' by now (attempt 1)
            lines = [x.rstrip() for x in w.dev.since(bm)]
            if not _has(lines, "[WIFI] Fix:  ?WIFI,TOKEN,<token>   then reboot"):
                problems.append("the boot refusal does not give the fix (?WIFI,TOKEN,<token>)")
            if not _has(lines, "7.0.1 joined without a token"):
                problems.append("the boot refusal does not say 7.0.1 joined without a token")
            if _has(lines, "[WIFI] will join") or _has(lines, "[WIFI] still looking for"):
                problems.append("with no token the JOIN still started")
            st = _status(w)
            for k, want in (("Mode", "JOIN"), ("Interface", "down"), ("WS endpoint", "NOT RUNNING")):
                if st.get(k) != want:
                    problems.append(f"with JOIN refused, {k} is {st.get(k)!r}, expected {want!r}")
            if not st.get("Token", "").startswith("none - JOIN will not start"):
                problems.append(f"with JOIN refused, the Token line is {st.get('Token')!r}")
            radio = st.get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"with JOIN refused the radio line is {radio!r}")
            _unicast_ok(w, w2s2, problems, "with JOIN refused")
        finally:
            if changed:
                _restore_ap(w, tok, ssid, problems)
                _token_put_back(w, before[1], problems)
        if _status(w).get("Mode") != "AP":
            problems.append("W1 is not back in AP mode")
    assert not problems, "; ".join(problems)


@test("wifi.channel_drift_warned", "With W1's access point up, the drift check names a radio off the mesh channel: ?DEBUG,FAULT,CHANDRIFT moves the radio one channel off for a single check, which prints '[WIFI] WARNING: radio is on channel x but the mesh is on y — this board cannot reach the mesh.', and back ('radio back on channel y'); ?WIFI's radio line is then on the mesh channel and a unicast to W2 is delivered", needs=["wcb1"], links=["W2S2"])
def channel_drift_warned(bench):
    """Coverage re-scan 2026-10-12, wifi.channel_drift_check. wcbWifiService (WCB_WiFi.cpp) compares the radio's
    channel with the mesh channel every CHAN_CHECK_MS (30 s) while an interface is up, and warns, since a drifted
    radio drops every ESP-NOW frame with no other symptom. Nothing on the bench moves it, so the CHANDRIFT knob
    (WCB_Fault.h) really moves the radio (esp_wifi_set_channel) for one check that runs at once, and moves it back
    right after: W1 is off the mesh channel for one loop pass. The check is reached only outside a pending JOIN, so
    this runs on W1's own access point, with no station on it (esp_wifi_set_channel may not move a SoftAP that has
    one). Nothing is saved."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        _, mode1, _, _ = _wifi_token(before[1])
        if mode1 != "AP":
            raise Skip("W1 does not host an access point")
        ch = _mesh_channel(before[1])
        st = _status(w)
        if st.get("Interface") != "up":
            raise Skip(f"W1's access point is not up (Interface {st.get('Interface')!r})")
        if not re.match(r"^0\b", st.get("Clients", "0")):
            raise Skip(f"a station is on W1's access point (Clients {st.get('Clients')!r})")
        try:
            m = w.dev.mark()
            fault(w, "CHANDRIFT")
            try:
                g = w.dev.expect(r"^\[FAULT\] CHANDRIFT: radio moved to channel (\d+) for one drift check$", timeout=5,
                                 since=m)
                off = g.group(1)
                try:
                    w.dev.expect(rf"^\[WIFI\] WARNING: radio is on channel {off} but the mesh is on {ch} — this board "
                                 rf"cannot reach the mesh\.$", timeout=3, since=m)
                except AssertionError:
                    problems.append(f"no drift WARNING naming channel {off} and the mesh's {ch}")
                try:
                    w.dev.expect(rf"^\[FAULT\] CHANDRIFT: radio back on channel {ch}$", timeout=3, since=m)
                except AssertionError:
                    problems.append(f"the radio did not report being back on channel {ch}")
            except AssertionError:
                said = [x.rstrip() for x in w.dev.since(m) if "CHANDRIFT" in x]
                problems.append(f"the knob did not move the radio: {said}")
            time.sleep(1.0)
            radio = _status(w).get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"after the drift check the radio line is {radio!r}")
            _unicast_ok(w, w2s2, problems, "after the drift check")
        finally:
            fault_off(w)
    assert not problems, "; ".join(problems)


# ============================================================ the PC on the AP (attended)
# The PC's side - netsh, the temporary profile, the lease wait, the routes - is hil/wlan.py (NAVICORE.md INF5), which
# suites/s45_navicore_wifi.py shares.
# W1's own WebSocket token while _pc_on_w1_ap's block runs (read from W1's chain; None when W1 has none): the clients
# _ws_open and _w1_identify open sign in with it when W1 asks (hil/ws.py sign_in_if_asked), and only then.
_W1_TOKEN = {"token": None}
_W1 = object()          # _ws_open's default: W1's own token


@contextlib.contextmanager
def _pc_on_w1_ap(bench, problems):
    """The PC on W1's access point for the block (_pc_on_ap) -> (w, W1's address, the adapter's name). Skip unless W1
    hosts a named, password-protected access point that is up with its WebSocket endpoint running. Off Windows only
    with bench.json "wifi_switch": "realtek" (hil/wlan.py realtek_on_ap): the Mac's spare adapter is moved to W1's
    access point, proved W1's by a ;S0 marker that comes out on W1's USB (_w1_identify), and put back on NaviCore's
    (navicore_home) afterwards."""
    if os.name != "nt" and not _realtek_enabled(bench):
        raise Skip("netsh (Windows) drives the PC's WiFi here; off Windows set bench.json wifi_switch to realtek")
    w = usb_wcb(bench)
    toks = bench.config_tokens(1, refresh=True)
    _, mode, ssid, pw = _wifi_token(toks)
    if mode != "AP" or not pw or not ssid:
        raise Skip("W1 does not host a named access point with a password")
    st = _status(w)
    if st.get("Interface") != "up" or not st.get("WS endpoint", "").startswith("ws://"):
        raise Skip(f"W1's access point is not up: Interface {st.get('Interface')!r}, WS {st.get('WS endpoint')!r}")
    ip = st.get("IP address", "")
    _W1_TOKEN["token"] = ws_token(toks)
    try:
        with _pc_on_ap(bench, problems, ssid, pw, "W1's", identify=_w1_identify(w), home=_navicore_home(bench)) as name:
            yield w, ip, name
    finally:
        _W1_TOKEN["token"] = None


def _w1_identify(w):
    """pc_on_ap's identify for W1's access point: None when a ;S0 marker sent to ws://192.168.4.1/ws through the
    adapter comes out on W1's own USB console, else what happened instead. Every board's access point is 192.168.4.1,
    and NaviCore's endpoint takes the same connect: only W1's console can say it was W1."""
    def identify(name):
        try:
            c = WsClient("192.168.4.1", timeout=4.0, token=_W1_TOKEN["token"])
        except AuthFailed:
            return "ws://192.168.4.1/ws refused W1's WebSocket token"
        except OSError as e:
            return f"ws://192.168.4.1/ws took no connect ({e})"
        try:
            mk = marker("AP")
            m = w.dev.mark()
            c.send_text(f";S0,{mk}\n")
            try:
                w.dev.expect(rf"^{mk}", timeout=3, since=m)
                return None
            except AssertionError:
                return "a ;S0 marker sent to ws://192.168.4.1/ws never came out on W1's console"
        except OSError as e:
            return f"the socket to 192.168.4.1 failed ({e})"
        finally:
            c.close()
    return identify


def _ws_open(ip, wait=15.0, token=_W1):
    """A WsClient to ws://<ip>/ws, retried for `wait` s (the lease is already held) -> the client, or None. It signs in
    if the board asks (hil/ws.py): with W1's own token by default, with `token`, or never (None). A token the board
    refuses raises AuthFailed at once: a retry would only fail again, inside the 1 s lockout."""
    if token is _W1:
        token = _W1_TOKEN["token"]
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            return WsClient(ip, timeout=4.0, token=token)
        except AuthFailed:
            raise
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


WS_MAX_CLIENTS = 2      # WCB_WS.cpp: httpd's max_open_sockets with LRU purge; 3 until tracker #111 (2026-10-07)


def _ws_client_count(w, timeout=5.0):
    m = re.search(r"\((\d+) client\(s\) connected\)$", _status(w, timeout).get("WS endpoint", ""))
    return int(m.group(1)) if m else None


def _chain_names_lost(full, short):
    """The tokens `full` has and `short` lacks, by name only ('?MAESTRO', '?SEQ,SAVE,Leia', at most 6) - the chains
    carry W1's mesh password and WiFi passphrase. Split on '^', a sequence's own '^' leaves fragments that do not start
    with the chain's function character: those are counted, never named."""
    from suites.s03_wcb import _names
    from suites.s05_mesh import CHAIN_TAIL

    def toks(c):
        m = CHAIN_TAIL.match(c or "")
        return (m.group(1) if m else (c or "")).split("^")
    lead = (full or "?")[:1]
    have = set(toks(short))
    gone = [t for t in toks(full) if t not in have]
    named = [t for t in gone if t.startswith(lead)]
    rest = len(gone) - len(named)
    return f"{len(gone)} token(s): {', '.join(_names(named)) or 'none named'}" + (f" (+{rest} fragment(s))" if rest else "")


def _heap_note(w):
    """'free heap 18044 bytes (min since boot 7336)' from W1's ?WIFI block."""
    try:
        return f"free heap {_status(w).get('Free heap', '?')}"
    except AssertionError:
        return "printed no ?WIFI block"


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
            for attempt in (1, 2):
                ws.text, ws.text_frames = "", []
                ws.send_text("?backup\n")
                if not _ws_until(ws, lambda t: "End of Backup" in t, 20):
                    problems.append(f"?backup over the WebSocket did not finish ({len(ws.text)} characters in 20 s)")
                chain = _live_chain([x.rstrip() for x in ws.text.split("\n")], False)
                # printBackupConfig leaves out a line it could not build for lack of heap, checksums what it did
                # print and says so right after: '... WARNING: N command(s) could not be built (out of memory) -
                # this backup is INCOMPLETE. Run ?backup again.' (F19). That is W1's heap with the access point up
                # and a client on it (tracker #111), not the tee: do what the warning says, once, and note it.
                short = [x.strip() for x in ws.text.split("\n") if "could not be built (out of memory)" in x]
                if not (short and _chain_ok(chain) and chain != usb_chain) or attempt == 2:
                    break
                bench.note(f"?backup over WS, try 1: W1 said {short[0][:110]!r}; chain {len(chain)} of "
                           f"{len(usb_chain)} characters, lacking {_chain_names_lost(usb_chain, chain)}; W1 "
                           f"{_heap_note(w)} - asking again, as the warning says")
            if not _chain_ok(chain):
                problems.append(f"the WebSocket's chain ({len(chain or '')} characters) fails its CRC")
            elif chain != usb_chain:
                said = f"; W1 said {short[0][:110]!r}" if short else " and W1 printed no out-of-memory warning"
                problems.append(f"the WebSocket's chain differs from USB's ({len(chain)} vs {len(usb_chain)} "
                                f"characters, lacking {_chain_names_lost(usb_chain, chain)}){said}; W1 "
                                f"{_heap_note(w)}")
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


def _heap_free(w):
    """W1's free byte-addressable heap from ?STATS ('Heap: N free, largest block M, ...'), or None."""
    for x in w.run("?STATS", timeout=30):
        m = re.match(r"^Heap: (\d+) free", x.strip())
        if m:
            return int(m.group(1))
    return None


@test("ws.vanished_client_released", "(should) OPT-IN (wifi_pc): a client that leaves W1's access point with its "
      "WebSocket still open is dropped as it leaves, and the heap its undelivered output held comes back: W1 counts no "
      "client within 10 s, and 10 s later its free heap is within 3 KB of where it started", needs=["wcb1"], links=[],
      opt_in="wifi_pc")
def ws_vanished_client_released(bench):
    """HIL tracker #113. A WebSocket whose PC left the access point without closing it - a phone out of range, a PC
    moved to another network - stays open on W1 while TCP retries it, and sinkPump keeps teeing the console to it: each
    send blocked loop() for httpd's send timeout (5 s) and the undelivered output stayed in lwIP, up to a TCP send
    buffer (~5.7 KB) per session. In run 20261007-140804 two or three such sessions left W1 with 4.8 KB free, largest
    block 980 B, nothing connected, and no client could connect at all. Here the socket is left open on purpose, the
    adapter leaves (pc_on_ap's way back), and W1 prints ?backup and ?config into the departed client."""
    problems = []
    w = usb_wcb(bench)
    heap0 = _heap_free(w)
    left = None
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        ws.send_text("?VERSION\n")
        ws.read_until("End of Version", timeout=6)
        if (_ws_client_count(w) or 0) < 1:
            problems.append("W1 counts no WebSocket client while one is open")
        left = ws                                  # never closed: the adapter leaves with it open
    stalls = []
    try:
        t0 = time.monotonic()
        for cmd in ("?backup", "?config"):             # a plain answer takes well under a second
            t = time.monotonic()
            w.run(cmd, timeout=30)
            if time.monotonic() - t > 3.0:
                stalls.append(f"{cmd} took {time.monotonic() - t:.1f} s")
        n = None
        while time.monotonic() - t0 < 10:
            n = _ws_client_count(w, timeout=30)
            if n == 0:
                break
            time.sleep(1)
        gone = time.monotonic() - t0
        time.sleep(10)
        heap1 = _heap_free(w)
    finally:
        try:
            left.sock.close()                      # this end only: its peer is on another network now
        except (OSError, AttributeError):
            pass
    bench.note(f"after the PC left with its socket open: W1 counted {n} client(s) {gone:.1f} s later; free heap "
               f"{heap0} before, {heap1} 10 s after that; slow answers: {stalls or 'none'}")
    if stalls:
        problems.append("W1's console stalled while it sent the departed client its output: " + ", ".join(stalls))
    if n != 0:
        problems.append(f"W1 still counts {n} WebSocket client(s) 10 s after the PC left with its socket open")
    if heap0 and heap1 is not None and heap1 < heap0 - 3000:
        problems.append(f"W1's free heap is {heap1} bytes, {heap0 - heap1} below the {heap0} it had before the client "
                        f"came: the departed client's session still holds it")
    assert not problems, "(should, #113) " + "; ".join(problems)


@test("ws.client_slots", "OPT-IN (wifi_pc): at most two WebSocket clients - a third evicts the least recently used, whose socket closes - each keeps its own line buffer, so a line split over frames on one client never fuses with another's, and a broadcast typed over the socket right after W2 sent W1 a command still reaches the mesh", needs=["wcb1", "wcb2"], links=["W1S2", "W2S3"], opt_in="wifi_pc")
def ws_client_slots(bench):
    """WCB-WP22 row 3 (ws.clients_slots_and_source_flags). wcbWsBegin sets max_open_sockets to WS_MAX_CLIENTS (2 since
    tracker #111) with lru_purge_enable, so httpd closes the least recently used session to take a third; accFor keeps
    one accumulator per socket (WCB_WS.cpp), so interleaved partial lines from two clients stay apart. sinkPump sends
    the console to every client, so each open client sees every answer: two commands, two answers on each. The source
    flags: wcbWsService clears lastReceivedViaESPNOW and inSequenceBody before it runs a socket's line, as the USB
    reader does, so a broadcast typed there right after a mesh-received command (;W1;S2 from W2's console) is not taken
    for a received one and dropped from the mesh: it reaches W2 S3, the port that takes broadcasts on W2."""
    s2, w2s3 = link(bench, 1, "S2"), link(bench, 2, "S3")
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        clients = []
        try:
            for k in range(WS_MAX_CLIENTS):
                c = _ws_open(ip)
                assert c is not None, f"client {k + 1} could not open ws://{ip}/ws with a lease held"
                clients.append(c)
                opened = time.monotonic()
                c.send_text("?VERSION\n")
                try:
                    c.read_until("End of Version", timeout=6)
                except (ConnectionError, OSError) as e:     # say which client W1 closed, when, and what it held then
                    after = time.monotonic() - opened
                    try:
                        st = _status(w)
                        held = f"W1 then reported {st.get('WS endpoint')!r}, free heap {st.get('Free heap')!r}"
                    except AssertionError:
                        held = "W1 then printed no ?WIFI block"
                    raise AssertionError(f"W1 closed client {k + 1} of {WS_MAX_CLIENTS} {after:.1f} s after it "
                                         f"opened, before ?VERSION's answer ended ({len(c.text)} characters had "
                                         f"arrived: {e}); {held}") from e
            n = _ws_client_count(w)
            if n != WS_MAX_CLIENTS:
                problems.append(f"with {WS_MAX_CLIENTS} clients open W1 counts {n}")
            extra = _ws_open(ip, wait=8)
            assert extra is not None, "a client over the limit could not connect at all"
            clients.append(extra)
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
                problems.append("the first client still answers after one over the limit connected: nothing was evicted")
            n = _ws_client_count(w)
            if n != WS_MAX_CLIENTS:
                problems.append(f"after one over the limit connected W1 counts {n}, not {WS_MAX_CLIENTS}")
            a, b = clients[-2], clients[-1]                  # the two still open: the newest two
            _ws_until(a, lambda t: False, 0.5)
            _ws_until(b, lambda t: False, 0.5)
            a.text, b.text = "", ""
            a.send_text("?VER")
            b.send_text("?VERSION\n")
            time.sleep(0.3)
            a.send_text("SION\n")
            for c, who in ((a, "older open"), (b, "newest")):
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


def _drops(lines, what):
    """The total N of W1's '[WS] dropped N <what>' report lines (wcbWsService, WCB_WS.cpp)."""
    return sum(int(m.group(1)) for x in lines for m in [re.match(rf"^\[WS\] dropped (\d+) {what}", x)] if m)


@test("ws.drop_reports", "OPT-IN (wifi_pc): the WebSocket endpoint reports what it drops. With W1's loop held by a slow ;S3 write (S3 at 600 baud, nothing printed), one frame of 40 ;S0 lines overflows the 6-deep inbound queue: every line either runs or is counted by '[WS] dropped N inbound command(s) — queue full or oversized', and six run. With the loop held again under ?DEBUG,ON, 40 long lines typed on USB make the serial task print 'Processing input from Serial0: ...' into a full output sink: '[WS] dropped N output line(s) — client too slow'.", needs=["wcb1"], links=["W1S3"], opt_in="wifi_pc")
def ws_drop_reports(bench):
    """Coverage re-scan 2026-10-12, ws.queue_backpressure and ws.output_tee. wsHandler (httpd task) feeds each line to
    accFeed, which waits WS_ENQUEUE_WAIT_MS (50 ms) for room in the 6-deep queue and counts a refusal (cmdDrops); the
    loop runs one queued WebSocket line per pass, and no pass comes while the loop is held. The tee (wcbWsSinkWrite)
    flushes a full 2 KB sink inline only on the loop task; a line printed on any other task - here the serial reader's
    'Processing input from Serial0' under ?DEBUG,ON - that finds it full drops whole and is counted (sinkDrops). Both
    counts are printed from loop() when it next runs wcbWsService.

    The loop is held by one long ;S3 line, whose soft-serial write waits for the wire at 600 baud (s18 HOLD_BAUD; at
    9600 the write returns at once and nothing is held, run 20261013-084347). The first cut held it with 400 ?VERSION,
    which mirrored ~16 KB of replies to the client, and with W2's unicasts under ?DEBUG,ETM: both times W1 closed the
    live session (sinkPump closes a session whose async send fails, #113) and the unicasts tripped the transmit-stall
    report while the held loop could not ACK them (runs 20261013-082616, -084533). ?DEBUG and S3's baud are put back."""
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        base_s3 = token(snapshot(bench, 1), "?BAUD,S3,") or "?BAUD,S3,9600"

        def drain(why, fatal=True):
            try:
                _ws_until(ws, lambda t: False, 3.0)
                return True
            except ConnectionError:
                if fatal:
                    problems.append(f"W1 closed the WebSocket {why}")
                else:   # re-scan row ws.burst_closes_live_client: noted, not this test's claim
                    bench.note(f"ws.drop_reports: W1 closed the live WebSocket {why} (sinkPump's close on a failed "
                               f"send after the burst; row ws.burst_closes_live_client)")
                return False
        try:
            if not any(f"Baud rate for Serial3 updated to {HOLD_BAUD}" in x for x in w.run(f"?BAUD,S3,{HOLD_BAUD}")):
                raise AssertionError(f"setup: ?BAUD,S3,{HOLD_BAUD} did not confirm")
            # (a) the inbound queue
            tag = marker("Q")
            m = w.dev.mark()
            w.dev.send(_hold_line(6.0))                      # holds W1's loop ~6 s, printing nothing
            time.sleep(0.6)
            ws.send_text("".join(f";S0{tag}{k:02d}\n" for k in range(40)))
            w.run(f";S0{marker('E')}", timeout=20)          # answers once the hold is over
            alive = drain("after the inbound burst")
            out = [x.rstrip() for x in w.dev.since(m)]
            ran = {k for k in range(40) if f"{tag}{k:02d}" in out}
            dropped = _drops(out, r"inbound command\(s\)")
            bench.note(f"ws.drop_reports inbound: {len(ran)} of 40 lines ran, {dropped} reported dropped")
            if not dropped:
                problems.append(f"no '[WS] dropped N inbound command(s)' line ({len(ran)} of 40 lines ran)")
            if len(ran) + dropped != 40:
                problems.append(f"{len(ran)} lines ran and {dropped} were reported dropped, of 40")
            # (b) the output tee
            if alive:
                tag2 = marker("T")
                w.run("?DEBUG,ON")
                m = w.dev.mark()
                w.dev.send(_hold_line(6.0))
                time.sleep(0.5)
                for k in range(40):                          # each printed by the serial task while the loop is held
                    w.dev.send(f";S0{tag2}{k:02d}{'z' * 100}")
                w.run(f";S0{marker('E')}", timeout=25)
                w.run("?DEBUG,OFF")
                drain("after the output sink overflowed", fatal=False)   # W1 may drop the client here (ws.burst_closes_live_client)
                out = [x.rstrip() for x in w.dev.since(m)]
                printed = sum(1 for x in out if x.startswith(f"Processing input from Serial0: ;S0{tag2}"))
                dropped = _drops(out, r"output line\(s\) — client too slow")
                bench.note(f"ws.drop_reports tee: {printed} 'Processing input' lines on USB, {dropped} output line(s) "
                           f"reported dropped")
                if not printed:
                    problems.append("W1 printed no 'Processing input from Serial0' line under ?DEBUG,ON")
                if not dropped:
                    problems.append("no '[WS] dropped N output line(s) — client too slow' line")
        finally:
            w.run("?DEBUG,OFF")
            w.run(base_s3, timeout=20)                       # after any hold still running
            ws.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


def _line_name(x):
    """A console line as a test may print it: a command or a chain (W1's ?backup carries its mesh password and WiFi
    passphrase) by its verb only, anything else cut to 50 characters."""
    return x.split(",")[0][:24] + ",..." if (x.startswith("?") or "^" in x) else x[:50]


def _ws_lacks(usb, got):
    """The console lines `got` (the socket's lines) lacks, matched in order: what the tee did not deliver."""
    k, lacking = 0, []
    for x in usb:
        j = k
        while j < len(got) and got[j] != x:
            j += 1
        if j == len(got):
            lacking.append(x)
        else:
            k = j + 1
    return lacking


def _ws_open_slow(ip, rcvbuf, wait=15.0):
    """A WsClient whose receive buffer is `rcvbuf` bytes, set before connecting: once it stops reading, its window
    fills within a few KB (hil/ws.py)."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            return WsClient(ip, timeout=4.0, rcvbuf=rcvbuf, token=_W1_TOKEN["token"])
        except AuthFailed:
            raise
        except OSError:
            time.sleep(1.5)
    return None


def _hold_s3(w, base_s3):
    if not any(f"Baud rate for Serial3 updated to {HOLD_BAUD}" in x for x in w.run(f"?BAUD,S3,{HOLD_BAUD}")):
        raise AssertionError(f"setup: ?BAUD,S3,{HOLD_BAUD} did not confirm")


@test("ws.burst_keeps_live_client", "OPT-IN (wifi_pc): a live WebSocket client on W1's access point stays connected through a burst of console output. With W1's loop held ~4 s by a slow ;S3 line, four ?backup typed on USB run back to back when it ends (~12 KB, each reply bigger than the 2 KB sink): the client gets every console line in order, or W1 reports what it dropped ('[WS] dropped N output line(s)'), it still answers a ;S0 sent over the socket afterwards, and W1's console never pauses over 1.5 s in the burst", needs=["wcb1"], links=["W1S3"], opt_in="wifi_pc")
def ws_burst_keeps_live_client(bench):
    """Re-scan row ws.burst_closes_live_client: found 2026-10-13 building ws.drop_reports, where a 10-16 KB burst
    closed a live client three times (runs 20261013-082616, -084533, -084748) and stalled the loop 3.5 s. WCB_WS.cpp's
    sender never blocks since 2026-10-14: send(MSG_DONTWAIT) writes what each socket takes, each client keeps its
    place in the frame in flight, 'no room' and 'no memory' (ENOMEM: W1's heap with the AP up, #111) mean try again
    on a later pass, and only a hard socket error or WS_STUCK_MS (10 s) without taking a byte closes a client. A full
    sink on the loop task waits for a client still taking bytes, at most WS_PASS_WAIT_MS (600 ms) per loop() pass.
    The backups carry W1's credentials: a line the socket lacks is named by its verb only."""
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        base_s3 = token(snapshot(bench, 1), "?BAUD,S3,") or "?BAUD,S3,9600"
        try:
            _hold_s3(w, base_s3)
            _ws_quiet(ws, quiet=0.6, cap=4)
            ws.text, ws.text_frames = "", []
            m = w.dev.mark()
            w.dev.send(_hold_line(4.0))                      # holds W1's loop ~4 s, printing nothing
            time.sleep(0.5)
            for _ in range(4):
                w.dev.send("?backup")                        # queued; run back to back once the hold ends
            end = marker("E")
            w.run(f";S0{end}", timeout=40)
            closed = None
            try:
                _ws_until(ws, lambda t: end in t, 15)
            except (ConnectionError, OSError) as e:
                closed = str(e)
            timed = [(t, x.rstrip()) for t, x in w.dev.since_timed(m) if x.strip()]
            starts = [i for i, (_, x) in enumerate(timed) if "WCB Configuration Backup" in x]
            ends = [i for i, (_, x) in enumerate(timed) if "End of Backup" in x]
            burst = timed[starts[0]:ends[-1] + 1] if starts and ends else timed
            gap = max((b[0] - a[0] for a, b in zip(burst, burst[1:])), default=0.0)
            usb = [x for _, x in burst if not x.startswith("[WS] ")]
            dropped = _drops([x for _, x in timed], r"output line\(s\) — client too slow")
            lacking = _ws_lacks(usb, [x.rstrip() for x in ws.text.split("\n") if x.strip()]) if not closed else usb
            bench.note(f"ws.burst_keeps_live_client: {len(ends)} backups, {sum(len(x) + 2 for x in usb)} console "
                       f"bytes in {burst[-1][0] - burst[0][0] if burst else 0:.1f} s, {len(ws.text_frames)} frame(s); "
                       f"longest console pause {gap:.2f} s; socket lacks {len(lacking)} line(s), W1 reported "
                       f"{dropped} dropped; W1 {_heap_note(w)}")
            if len(ends) != 4:
                problems.append(f"{len(ends)} of the four ?backup finished on USB")
            if closed:
                problems.append(f"W1 closed the live client during the burst ({closed})")
            else:
                alive = marker("A")
                ws.text = ""
                ws.send_text(f";S0{alive}\n")
                try:
                    if not _ws_until(ws, lambda t: alive in t, 5):
                        problems.append("after the burst the client got no answer to a ;S0 it sent")
                except (ConnectionError, OSError) as e:
                    problems.append(f"W1 closed the client just after the burst ({e})")
            if lacking and not dropped and not closed:
                problems.append(f"the socket lacks {len(lacking)} console line(s) W1 never reported dropping "
                                f"(first: {_line_name(lacking[0])!r})")
            if gap > 1.5:
                problems.append(f"W1's console paused {gap:.2f} s in the burst: the loop waited on the socket")
        finally:
            w.run(base_s3, timeout=20)                       # after any hold still running
            ws.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("ws.ping_pong_and_close", "OPT-IN (wifi_pc): W1 answers a WebSocket PING with a PONG carrying the same payload, sent between data frames: a PING sent while ?backup streams out gets its PONG and leaves every frame whole (the chain passes its CRC, every text frame is valid UTF-8), an empty PING gets an empty PONG, and a CLOSE is echoed and leaves W1 counting no client", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ws_ping_pong_and_close(bench):
    """WCB_WS.cpp registers /ws with handle_ws_control_frames (2026-10-14). Before, httpd answered PING and CLOSE
    itself with a blocking send from its own task, which could land inside a frame the loop's non-blocking sender had
    half written; now wsHandler reads every frame's payload, queues the PONG for sinkPump to send at the next frame
    boundary (queuePong), and echoes a CLOSE itself when no frame is part-way out (closeEcho) before httpd closes the
    session. ?backup's chain carries W1's credentials: only its CRC is checked, never shown."""
    from suites.s05_mesh import _chain_ok, _live_chain
    problems = []
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open(ip)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        try:
            _ws_quiet(ws, quiet=0.6, cap=4)
            ws.text, ws.text_frames, ws.pongs = "", [], []
            ws.send_text("?backup\n")
            time.sleep(0.05)
            ws.send_ping(b"HILPING")
            if not _ws_until(ws, lambda t: "End of Backup" in t and bool(ws.pongs), 20):
                problems.append(f"?backup with a PING sent into it: {'no PONG' if not ws.pongs else 'no End of Backup'} "
                                f"in 20 s ({len(ws.text)} characters)")
            _ws_quiet(ws, quiet=0.8, cap=4)
            if ws.pongs != [b"HILPING"]:
                problems.append(f"the PING sent into ?backup got {ws.pongs!r} back, not one PONG b'HILPING'")
            chain = _live_chain([x.rstrip() for x in ws.text.split("\n")], False)
            if not _chain_ok(chain):
                problems.append(f"with a PONG sent among ?backup's frames the chain ({len(chain or '')} characters) "
                                f"fails its CRC")
            bad = 0
            for f in ws.text_frames:
                try:
                    f.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    bad += 1
            if bad:
                problems.append(f"{bad} of {len(ws.text_frames)} text frame(s) are not valid UTF-8 on their own")
            n = len(ws.pongs)
            ws.send_ping(b"")
            _ws_until(ws, lambda t: len(ws.pongs) > n, 3)
            if ws.pongs[n:] != [b""]:
                problems.append(f"an empty PING got {ws.pongs[n:]!r} back, not one empty PONG")
            ws.sock.sendall(ws_frame(struct.pack(">H", 1000), opcode=0x8))
            gone = False
            try:
                ws.read_until("\x00never\x00", timeout=3)
                ws.sock.settimeout(3)
                ws._frame()                                  # after the echo: the socket closes
            except (ConnectionError, OSError):
                gone = True
            if not ws.close_frames:
                problems.append("W1 did not echo the CLOSE frame")
            if not gone:
                problems.append("the socket stayed open 3 s after the CLOSE")
            time.sleep(1.0)
            k = _ws_client_count(w)
            if k != 0:
                problems.append(f"after the CLOSE W1 counts {k} WebSocket client(s)")
            bench.note(f"ws.ping_pong_and_close: {len(ws.text_frames)} frame(s) of ?backup, PONGs {ws.pongs!r}, "
                       f"CLOSE echoed {bool(ws.close_frames)}")
        finally:
            ws.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


def _free_heap(w):
    """W1's free heap in bytes from its ?WIFI block, or None."""
    m = re.match(r"^(\d+) bytes", _status(w).get("Free heap", ""))
    return int(m.group(1)) if m else None


def _heap_back(w, heap0, within=20.0, slack=3000):
    """Poll W1's free heap until it is within `slack` bytes of `heap0` -> (back?, last reading, seconds waited)."""
    t0 = time.monotonic()
    last = None
    while True:
        last = _free_heap(w)
        if heap0 is None or (last is not None and last >= heap0 - slack):
            return True, last, time.monotonic() - t0
        if time.monotonic() - t0 >= within:
            return False, last, time.monotonic() - t0
        time.sleep(2.0)


def _fill_until_dropped(w, since, most=50):
    """?backup on USB until W1 reports a dropped WebSocket line after `since` (the client's window and W1's send buffer
    are full), at most `most` times -> (each ?backup's seconds, whether a drop was reported). The Mac's receive buffer
    took about 40 KB before the first drop (run 20261014-021636), and W1 reports drops at most once a second."""
    took = []

    def dropped():
        return _drops([x.rstrip() for x in w.dev.since(since)], r"output line\(s\) — client too slow")
    for _ in range(most):
        t0 = time.monotonic()
        w.run("?backup", timeout=15)
        took.append(time.monotonic() - t0)
        if dropped():
            return took, True
    time.sleep(1.5)
    return took, bool(dropped())


@test("ws.stalled_client_costs_nothing", "OPT-IN (wifi_pc): a WebSocket client that stops reading costs W1 nothing. With the client not reading, ?backup typed on USB until its window and W1's send buffer are full (W1 reports '[WS] dropped N output line(s)') each finishes within 1.6 s; reading again, the client is still connected and gets the answer to a ;S0 it sends, and every console line it lacks was reported dropped. Stopped for good while W1 owes it output, it is closed about 10 s later with '[WS] closed 1 client(s) that took no output for 10 s', and W1 counts no client", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ws_stalled_client_costs_nothing(bench):
    """WCB_WS.cpp's sender (2026-10-14): a socket with no room returns EWOULDBLOCK at once, so loop() never waits on
    it; a full sink waits only for a client that took bytes in the last WS_FRESH_MS (300 ms), and at most
    WS_PASS_WAIT_MS (600 ms) a pass; WS_STUCK_MS (10 s) without a byte taken while it owes output closes the client -
    #113's dead peer when no station-left event says so. Before, each flush to such a client blocked loop() up to
    send_wait_timeout (2 s), and the first failure closed it. The client asks for a 2 KB receive buffer, but the Mac
    took 9 KB without a drop in the first cut (run 20261014-020343) and about 40 KB in the next, so the backups go on
    until W1 reports one. The backups' lines are named by verb only."""
    problems = []
    w1 = usb_wcb(bench)
    heap0 = _free_heap(w1)                                   # before the PC joins: what W1 has to come back to
    with _pc_on_w1_ap(bench, problems) as (w, ip, _):
        ws = _ws_open_slow(ip, 2048)
        assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
        try:
            _ws_quiet(ws, quiet=0.6, cap=4)
            ws.text = ""
            m = w.dev.mark()
            stopped = time.monotonic()
            took, full = _fill_until_dropped(w, m)           # the client reads nothing meanwhile
            waited = time.monotonic() - stopped
            if not full:
                raise Skip(f"{len(took)} ?backup ({len(took) * 3} KB or so) never filled the client's window: no "
                           f"drop to test")
            # WS_STUCK_MS counts from the last byte the client took - just before the first drop, about a second
            # before it was reported - so reading again now is well inside the 10 s.
            alive = marker("A")
            closed = None
            try:
                _ws_quiet(ws, quiet=1.0, cap=15)             # reading again: what W1 kept for it
                usb = [x.rstrip() for x in w.dev.since(m) if x.strip() and not x.startswith("[WS] ")]
                got = [x.rstrip() for x in ws.text.split("\n") if x.strip()]
                ws.text = ""
                ws.send_text(f";S0{alive}\n")
                if not _ws_until(ws, lambda t: alive in t, 5):
                    problems.append("reading again, the client got no answer to a ;S0 it sent")
            except (ConnectionError, OSError) as e:
                closed = str(e)
                usb, got = [], []
            out = [x.rstrip() for x in w.dev.since(m)]
            dropped = _drops(out, r"output line\(s\) — client too slow")
            lacking = _ws_lacks(usb, got)
            bench.note(f"ws.stalled_client_costs_nothing: {len(took)} ?backup took {', '.join(f'{t:.2f}' for t in took)} s "
                       f"with the client not reading ({waited:.1f} s in all); it lacked {len(lacking)} line(s), W1 "
                       f"reported {dropped} dropped")
            if closed:
                problems.append(f"W1 closed the client that had stopped reading for {waited:.1f} s ({closed})")
            slow = [f"{t:.2f}" for t in took if t > 1.6]
            if slow:
                problems.append(f"?backup took {', '.join(slow)} s with the client not reading: the loop waited on it")
            if lacking and not dropped:
                problems.append(f"the client lacks {len(lacking)} console line(s) W1 never reported dropping (first: "
                                f"{_line_name(lacking[0])!r})")
            if not closed:
                # Stopped for good: fill its window and W1's send buffer again, then let WS_STUCK_MS run out.
                m2 = w.dev.mark()
                _, full2 = _fill_until_dropped(w, m2)
                t_full = time.monotonic()
                if not full2:
                    problems.append("the second fill never filled the client's window: the stuck close is untested")
                else:
                    try:
                        hit = w.dev.expect(r"^\[WS\] closed (\d+) client\(s\) that took no output for (\d+) s",
                                           timeout=16, since=m2)
                        after = time.monotonic() - t_full
                        if hit.group(1) != "1":
                            problems.append(f"W1 closed {hit.group(1)} clients for taking no output, not 1")
                        bench.note(f"ws.stalled_client_costs_nothing: W1 said '{hit.group(0)}' {after:.1f} s after "
                                   f"the window was full")
                    except AssertionError:
                        problems.append("W1 never closed the client that stopped reading for good (no '[WS] closed N "
                                        "client(s) that took no output' within 16 s of its window filling)")
                    k = _ws_client_count(w)
                    if k != 0:
                        problems.append(f"after the stuck client was closed W1 counts {k} WebSocket client(s)")
        finally:
            try:
                ws.sock.close()
            except OSError:
                pass
    _left_clean(bench, w, problems)
    back, last, took_s = _heap_back(w, heap0)
    bench.note(f"ws.stalled_client_costs_nothing: W1's free heap {heap0} before the PC joined, {last} "
               f"{took_s:.0f} s after it left")
    if not back:
        problems.append(f"20 s after the PC left W1's free heap is {last} bytes, not back within 3 KB of the {heap0} it "
                        f"had before: the stalled client's session still holds memory (the next client cannot join)")
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


def _pc_reaches_joined_w1(bench, problems, ssid2, pw2, ip1, wtok):
    """WCB-WP22 row 4's second half, run by wifi.join_w2_ap when wifi_pc is ticked as well: the PC joins W2's access
    point too and opens ws://<W1's joined address>/ws, where ?VERSION is answered - W1's endpoint serves on a JOIN
    address, reached through W2's access point, and a JOIN board has a token, `wtok`, to sign in with. A Skip here (not
    Windows, no adapter) is noted, not raised: the JOIN half has run by then."""
    try:
        with _pc_on_ap(bench, problems, ssid2, pw2, "W2's"):
            try:
                ws = _ws_open(ip1, token=wtok)
            except AuthFailed:
                problems.append("W1's endpoint on its joined address refused its own WebSocket token")
                return
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


# ============================================================ the sign-in, seen from the socket (wifi_pc)
@contextlib.contextmanager
def _w1_token_on(w, before, problems):
    """A throwaway WebSocket token on W1 for the block -> the token; W1's own token state from its chain `before`
    afterwards. A token applies at once, to sessions opened after it (wcbWsSetToken, WCB_WS.cpp): no reboot."""
    t = _throwaway_token()
    _token_set(w, t)
    try:
        yield t
    finally:
        _token_put_back(w, before, problems)


def _auth_lines(ws):
    return [x.strip() for x in ws.text.splitlines() if x.strip()]


@test("ws.token_signed_out", "OPT-IN (wifi_pc): with a WebSocket token set on W1, a session that has not signed in is sent nothing but [AUTH:REQUIRED] - one when it opens and one for a line it sends, which does not run (its ;S0 marker never reaches W1's console) - and none of W1's console (a ?VERSION typed on USB is not mirrored to it); W1's own token state is put back", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ws_token_signed_out(bench):
    """docs/WIFI_DESIGN.md §7. sinkAdd (WCB_WS.cpp) opens a session outside the tee when a token is set and owes it
    [AUTH:REQUIRED]; wsAdmit drops every line it sends before ?AUTH, unqueued, and owes another; sinkPump opens console
    frames only for signed-in clients (txInTee). The token is throwaway, set and taken away over USB."""
    problems = []
    with config_guard(bench, 1) as before, _pc_on_w1_ap(bench, problems) as (w, ip, _):
        with _w1_token_on(w, before[1], problems):
            ws = _ws_open(ip, token=None)
            assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
            try:
                if not _ws_until(ws, lambda t: AUTH_REQUIRED in t, 4):
                    problems.append("a new session was not sent [AUTH:REQUIRED]")
                mk = marker("TOK")
                m = w.dev.mark()
                ws.send_text(f";S0,{mk}\n")
                if not _ws_until(ws, lambda t: t.count(AUTH_REQUIRED) >= 2, 4):
                    problems.append("a line sent before signing in was not answered [AUTH:REQUIRED]")
                time.sleep(1.0)
                if any(mk in x for x in w.dev.since(m)):
                    problems.append("a line sent before signing in ran (its ;S0 marker reached W1's console)")
                w.run("?VERSION")
                _ws_quiet(ws, quiet=1.5, cap=5)
                got = _auth_lines(ws)
                other = [x for x in got if x != AUTH_REQUIRED]
                if other:
                    problems.append(f"a signed-out session was sent {len(other)} line(s) besides [AUTH:REQUIRED], "
                                    f"the first {other[0][:60]!r}")
                if got.count(AUTH_REQUIRED) != 2:
                    problems.append(f"{got.count(AUTH_REQUIRED)} [AUTH:REQUIRED] line(s), expected 2 (the open, the line)")
            finally:
                ws.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("ws.token_wrong_closes", "OPT-IN (wifi_pc): with a WebSocket token set on W1, a wrong ?AUTH is answered [AUTH:FAIL] and a CLOSE with status 1008; within 1 s of that failure the right token fails too, on a new session; after that the right token signs in and ?VERSION is answered; W1's own token state is put back", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ws_token_wrong_closes(bench):
    """docs/WIFI_DESIGN.md §7. wsAdmit (WCB_WS.cpp) compares in constant time; a mismatch, or any ?AUTH within
    WS_AUTH_LOCKOUT_MS (1 s) of a failure on any session, owes [AUTH:FAIL], and sinkPump follows it with a 1008 CLOSE
    and closes the session when the client echoes it (WS_BYE_WAIT_MS, 1 s, if it does not). The second session is
    opened at once so it lands inside the lockout; a WiFi round trip slow enough to miss it is noted, not failed."""
    problems, facts = [], {}
    with config_guard(bench, 1) as before, _pc_on_w1_ap(bench, problems) as (w, ip, _):
        with _w1_token_on(w, before[1], problems) as t:
            ws = _ws_open(ip, token=None)
            assert ws is not None, f"could not open ws://{ip}/ws with a lease held"
            second = None
            try:
                _ws_until(ws, lambda x: AUTH_REQUIRED in x, 4)
                t_fail = None
                try:
                    ws.sign_in("hil" + "x" * 9)
                    problems.append("a wrong token was answered [AUTH:OK]")
                except AuthFailed:
                    t_fail = time.monotonic()
                except ConnectionError as e:
                    problems.append(f"a wrong token was not answered [AUTH:FAIL] ({e})")
                ws.read_until("\x00never\x00", timeout=2)            # the CLOSE that follows
                if not ws.close_frames:
                    problems.append("no CLOSE followed [AUTH:FAIL]")
                elif ws.close_frames[0][:2] != CLOSE_POLICY:
                    problems.append(f"the CLOSE after [AUTH:FAIL] has status {ws.close_frames[0][:2].hex()}, not 03f0 "
                                    f"(1008)")
                if t_fail is not None:
                    second = WsClient(ip, timeout=4.0)
                    _ws_until(second, lambda x: AUTH_REQUIRED in x, 2)
                    try:
                        second.sign_in(t)
                        dt = time.monotonic() - t_fail
                        if dt < 0.8:
                            problems.append(f"the right token signed in {dt:.2f} s after a failure (the lockout is 1 s)")
                        else:
                            facts["lockout"] = f"not checked: the second session took {dt:.2f} s"
                    except AuthFailed:
                        facts["lockout"] = f"held {time.monotonic() - t_fail:.2f} s after the failure"
            finally:
                ws.close()
                if second is not None:
                    second.close()
            time.sleep(1.5)                                          # past the lockout
            try:
                good = _ws_open(ip, token=t)
            except AuthFailed:
                good = None
                problems.append("the right token was refused 1.5 s after the last failure")
            if good is not None:
                try:
                    if good.auth_lines[-1:] != [AUTH_OK]:
                        problems.append(f"the right token's sign-in ended {good.auth_lines[-1:]}, not [AUTH:OK]")
                    good.text = ""
                    good.send_text("?VERSION\n")
                    if not _ws_until(good, lambda x: "End of Version" in x, 6):
                        problems.append("signed in after the lockout, ?VERSION was not answered")
                finally:
                    good.close()
    bench.note(f"ws.token_wrong_closes: {facts}")
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)


@test("ws.token_signs_in", "OPT-IN (wifi_pc): with a WebSocket token set on W1, the right ?AUTH is answered [AUTH:OK], and the session then runs what it sends (?VERSION answered) and is sent W1's console (a ;S0 marker typed on USB arrives); a session open before the token was set stays signed in; with the token cleared again a new session is sent no [AUTH:REQUIRED] and ?AUTH,<anything> is answered [AUTH:OK]; W1's own token state is put back", needs=["wcb1"], links=[], opt_in="wifi_pc")
def ws_token_signs_in(bench):
    """docs/WIFI_DESIGN.md §7. wsAdmit (WCB_WS.cpp) signs a session in for its lifetime on a match and owes [AUTH:OK],
    which sinkPump sends ahead of the first console frame the session joins (txInTee); the token as it is when a
    session opens decides that session (sinkAdd), so one open before ?WIFI,TOKEN keeps going; with no token any ?AUTH
    answers [AUTH:OK]. Two clients at most (WS_MAX_CLIENTS), so the early one is closed before the last opens."""
    problems = []
    with config_guard(bench, 1) as before, _pc_on_w1_ap(bench, problems) as (w, ip, _):
        early = _ws_open(ip)                         # W1 as it is: no token, or signed in with its own
        assert early is not None, f"could not open ws://{ip}/ws with a lease held"
        late = None
        try:
            with _w1_token_on(w, before[1], problems) as t:
                early.text = ""
                early.send_text("?VERSION\n")
                if not _ws_until(early, lambda x: "End of Version" in x, 6):
                    problems.append("a session open before the token was set stopped running what it sends")
                try:
                    late = _ws_open(ip, token=t)
                except AuthFailed:
                    problems.append("the right token was answered [AUTH:FAIL]")
                if late is not None:
                    if late.auth_lines[-1:] != [AUTH_OK]:
                        problems.append(f"the sign-in ended {late.auth_lines[-1:]}, not [AUTH:OK]")
                    late.text = ""
                    late.send_text("?VERSION\n")
                    if not _ws_until(late, lambda x: "End of Version" in x, 6):
                        problems.append("signed in, ?VERSION was not answered")
                    mk = marker("TOK")
                    late.text = ""
                    w.run(f";S0,{mk}")
                    if not _ws_until(late, lambda x: mk in x, 4):
                        problems.append("signed in, W1's console was not mirrored to the session")
                    late.close()
                    late = None
                if not _has(w.run("?WIFI,TOKEN,CLEAR"), TOKEN_CLEARED):
                    problems.append("?WIFI,TOKEN,CLEAR did not confirm")
                early.close()
                early = None
                free = _ws_open(ip, token=None)
                if free is None:
                    problems.append("with the token cleared no session could be opened")
                else:
                    try:
                        if _ws_until(free, lambda x: AUTH_REQUIRED in x, 1.5):
                            problems.append("with the token cleared a new session was sent [AUTH:REQUIRED]")
                        try:
                            free.sign_in("anything-at-all")
                        except ConnectionError as e:
                            problems.append(f"with no token ?AUTH,<anything> was not answered [AUTH:OK] ({e})")
                        free.text = ""
                        free.send_text("?VERSION\n")
                        if not _ws_until(free, lambda x: "End of Version" in x, 6):
                            problems.append("with the token cleared, ?VERSION was not answered")
                    finally:
                        free.close()
        finally:
            for c in (early, late):
                if c is not None:
                    c.close()
    _left_clean(bench, w, problems)
    assert not problems, "; ".join(problems)
