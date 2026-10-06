"""The mesh password and the NVS erase (docs/HIL_TEST_AUDIT.md WP11). Two attended opt-ins, and the restore path they
depend on proved first without erasing anything:
- backup.replay_idempotent (no opt-in): W1's own config chain replayed one line at a time - exactly how the erase
  tests restore W1 - is accepted line by line and leaves the chain unchanged.
- mesh_password: ident.epass_live gives W1 a throwaway mesh password for a few seconds, then its own back.
- nvs_erase: nvs.erase_defaults_restore (?ERASE,NVS) and nvs.wcb_erase_alias (?WCB_ERASE) erase W1, check what a
  fresh board boots with, and restore it: ?HW and a boot first, then every line of the old chain, a boot, and the
  learned peers. The saved serial-attached devices are not config and cannot be put back; devices re-announce.
- wcb.nvs_report (no opt-in): ?NVS on W1 and W2 - format and arithmetic.
- nvs_fill: seq.nvs_full_consistency fills W1's NVS with throwaway sequences until a save is refused, and checks that
  nothing is left half-saved or listed empty (F9), then removes them; nvs.full_map_and_device_save fills it the same
  way and checks a serial mapping and the WDP-DA device list on the full store (WCB-WP42).
- backup.one_line_restore and backup.chain_restore_funcchar (no opt-in): W1's chains pasted back as one line, through
  the ?CHK gate (WCB-WP12).
- mesh_password: ident.epass_window_gates also takes W1 off the mesh for seconds, to show which receivers check the
  password (WCB-WP43). nvs_erase: nvs.erase_led_and_tails is a third erase with the LED pin and the erase's tails
  (WCB-WP57).

Secrets: the ?EPASS and ?WIFI lines are read from W1's own chain at run time and handed back to W1 only. They never go
into a note, a failure message or a file; the few messages that name a token pass it through the harness's
redaction (hil/checkpoint.py). The board itself echoes a new mesh password on its console, as it always has.

Restore if a run dies midway: W1's pre-test ?backup is in that run's session.log. On W1's USB console, paste its
"For Factory Reset/Fresh Boards" line (or send the "For Configured Boards" tokens one by one), then ?reboot, then
?WDP,ADD,<n> for each learned peer the log's first `learned peers` note lists. Nothing here moves a servo.
"""
import re
import time

from hil.checkpoint import redact_text, redact_token
from hil.runner import Skip, test
from hil.wcb import WCB, chain_crc, group_tokens, parse_pull_line
from suites.common import Console, config_guard, link, marker, nonce, padded, prime, snapshot, token, usb_wcb
from suites.s12_serial_input import _da_forget, _da_scrub, _da_types
from suites.s18_etm_config_wdp import _chains, _live_chars, _sent
from suites.s27_identity_destructive import _learned_peers
from suites.s03_wcb import CHK_LINE, _at, backup_chain_lines, backup_chain_problems
from suites.s29_leftovers import _crun, _cut, _has

# Output that means a replayed line was refused. Every line of a chain the board printed itself must be accepted.
_REFUSED = re.compile(r"Unknown command|Invalid|BLOCKED|REJECTED|[Rr]efus|not configured|too long|FAILED|failed|Error|ERROR")


def _replayable(tokens):
    """Skip unless the chain can be replayed a line at a time: a ?MAP,PWM line reboots the board mid-replay, and a
    changed delimiter or command character would change how the replay itself is read."""
    if any(t.upper().startswith("?MAP,PWM") for t in tokens):
        raise Skip("the chain holds a PWM mapping, which reboots the board mid-replay")
    for prefix, default in (("?DELIM,", "^"), ("?FUNCCHAR,", "?"), ("?CMDCHAR,", ";")):
        t = token(tokens, prefix)
        if t and t[len(prefix):] != default:
            raise Skip(f"{prefix[1:-1]} is not the default")


def _replay(w, tokens):
    """Send each chain line on its own, in chain order (?CHK and the read-only ?PEERSLIVE left out), a multi-step
    sequence's value whole (group_tokens: a raw '^' split saved its first step alone and ran the rest as commands).
    Returns the refused lines, redacted, so the caller can report them without quoting a credential."""
    bad = []
    for t in group_tokens(tokens):
        if t.upper().startswith(("?CHK", "?PEERSLIVE")):
            continue
        out = w.run(t, timeout=8)
        hit = next((x for x in out if _REFUSED.search(x)), None)
        if hit:
            bad.append(f"{redact_token(t)} -> {redact_text(hit.strip())}")
    return bad


def _da_rows(w):
    """(port, type) of every saved serial-attached device (?WDP,DA)."""
    rows = set()
    for x in w.run("?WDP,DA"):
        m = re.match(r"^  S(\d)  (.{1,24}?)\s+fw ", x)
        if m:
            rows.add((int(m.group(1)), m.group(2).strip()))
    return rows


def _mesh_both_ways(w, c2, problems, when):
    """W2 runs a command from W1, and W1 runs one from W2 (each prints a marker on the other board's USB)."""
    v = marker("v")
    m = c2.mark()
    w.send(f";W2,;S0{v}")
    try:
        c2.expect(rf"{v}$", timeout=6, since=m)
    except AssertionError:
        problems.append(f"{when}: W2 did not run a command from W1")
    u = marker("u")
    wm = w.dev.mark()
    _crun(c2, f";W1,;S0{u}", 2.5)
    if not any(x.strip() == u for x in w.dev.since(wm)):
        problems.append(f"{when}: W1 did not run a command from W2")


# ============================================================ the restore path, without erasing
@test("backup.replay_idempotent", "W1's own config chain replayed one line at a time, the way the erase tests restore W1, is accepted line by line and leaves the chain exactly as it was", needs=["wcb1"], links=[])
def replay_idempotent(bench):
    """Every line is one the board printed itself, so re-applying it must be a no-op. This is also what a full Wizard
    push of an unchanged board amounts to. No reboot: nothing in the chain changes, so nothing is waiting for one."""
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        _replayable(before[1])
        bad = _replay(w, before[1])
    assert not bad, "lines refused on replay: " + "; ".join(bad)


# ============================================================ ?EPASS
@test("ident.epass_live", "(attended) ?EPASS applies at once: with a throwaway mesh password W1 is neither heard (its unicast to W2 fails after 3 retries and never runs) nor listens (W2's command is ignored); W1's own password, from its chain, restores both ways; the legacy ?EPASS<pw> spelling and the argument errors", needs=["wcb1"], links=[], opt_in="mesh_password")
def epass_live(bench):
    """The ?EPASS handler (WCB.ino) calls setESPNowPassword (WCB_Storage.cpp), which writes NVS and the live
    espnowPassword every packet carries and every receiver checks. Error forms first, while nothing has changed."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before, Console(bench, 2) as c2:
        own = token(before[1], "?EPASS,")
        if own is None:
            raise Skip("W1's chain has no ?EPASS line to restore")
        for cmd, want in (("?EPASS", "Invalid format. Use: ?EPASS,password"),
                          ("?EPASS," + "x" * 40, "Password too long. Max 39 characters."),
                          ("?EPASS" + "y" * 40, "Invalid ESP-NOW password length. Must be between 1 and 39 characters.")):
            if not _has(w.run(cmd), want):
                problems.append(f"{cmd[:12]}... did not print {want!r}")
        changed = False
        try:
            w.run("?DEBUG,ETM,ON")
            tmp = marker("pw")                               # a throwaway, never a real credential
            out = w.run(f"?EPASS,{tmp}")
            changed = True
            if not _has(out, f"ESP-NOW password updated to: {tmp}"):
                problems.append("?EPASS,<throwaway> did not confirm")
            t = marker()
            wm, cm = w.dev.mark(), c2.mark()
            w.send(f";W2,;S0{t}")
            try:
                w.dev.expect(rf"\[ETM\] WCB2 failed to ACK seq \d+ after 3 retries: ;S0{t}", timeout=10, since=wm)
            except AssertionError:
                problems.append("with a wrong password, W1's unicast to W2 did not fail")
            if any(x.strip().endswith(t) for x in c2.lines(cm)):
                problems.append("with a wrong password, W2 still ran W1's command")
            u = marker("u")
            wm = w.dev.mark()
            _crun(c2, f";W1,;S0{u}", 2.5)
            if any(x.strip() == u for x in w.dev.since(wm)):
                problems.append("with a wrong password, W1 still ran W2's command")
            tmp2 = marker("pw")
            if not _has(w.run(f"?EPASS{tmp2}"), f"ESP-NOW Password updated to: {tmp2}"):
                problems.append("legacy ?EPASS<pw> did not confirm")
        finally:
            if changed:
                out = w.run(own)                             # W1's own line from its chain, back to W1 only
                if not _has(out, "ESP-NOW password updated to: "):
                    problems.append("replaying W1's own ?EPASS line did not confirm")
            w.run("?DEBUG,ETM,OFF")
        time.sleep(1)
        _mesh_both_ways(w, c2, problems, "after the restore")
    assert not problems, "; ".join(problems)


# ============================================================ ?ERASE,NVS / ?WCB_ERASE
def _defaults_problems(banner, toks, base, da_after, da_before):
    """What a fresh board boots with, from the firmware's own defaults: WCB 1 and a quantity of 1 (WCB.ino:139, :141),
    MAC octets 00 (:149-150), no controller (:145), '^' '?' ';' (:157-162), the ETM values (:303-310), WDP and auto-join
    on (WCB_WDP.cpp:51-52), channel 1 (WCB_Storage.h:25), every port 9600 with both broadcast directions on, no hardware
    version (the hw 0 lines). Since 2026-09-24 eraseNVSFlash (WCB_Storage.cpp) erases every namespace, so WiFi is off
    and no serial-attached device is saved (docs/HIL_TEST_AUDIT.md F8; they survived before)."""
    p = []
    want = ["Booting up the Wireless Communication Board 1 (W1)", "Number of WCBs in the system: 1",
            "ESP-NOW MAC Address: 02:00:00:00:00:01", "Added ESP-NOW broadcast peer: FF:00:00:FF:FF:FF",
            "SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!", "No LED was setup.  Define HW version",
            "Delimeter Character: ^", "Local Function Identifier: ?", "Command Character: ;", "ETM: ENABLED",
            "[WDP] discovery enabled", "Kyber is Not used", "  No Maestros configured",
            "  No serial mappings configured", "No input mappings configured", "Raw Serial Forwarding Task Created"]
    p += [f"after the erase the banner lacks {x!r}" for x in want if x not in banner]
    for prefix in ("Added ESP-NOW peer:", "Added ESP-NOW special peer", "[PEER] ", "Loaded WCB alias", "[HCR] Loaded",
                   "[MP3] Loaded", "[DFP] Loaded", "[WLED] Loaded", "[VAR] Loaded", "Maestro_Remote Task Created",
                   "Kyber_Local Task Created", "  Maestro "):
        if any(x.startswith(prefix) for x in banner):
            p.append(f"after the erase the banner still has a line starting {prefix!r}")
    for port in range(1, 6):
        head = f"  Serial{port} Baud: 9600, Broadcast Input: Enabled, Broadcast Output: Enabled  Pins: Tx:"
        if not any(x.startswith(head) for x in banner):
            p.append(f"after the erase S{port} is not at 9600 with both broadcast directions on")
    must = ["?HW,0", "?WCB,1", "?WCBQ,1", "?MAC,2,00", "?MAC,3,00", "?WCBCH,1", "?ETM,ON", "?ETM,TIMEOUT,500", "?ETM,HB,10",
            "?ETM,MISS,5", "?ETM,BOOT,2", "?ETM,COUNT,20", "?ETM,DELAY,100", "?ETM,CHKSM,ON", "?BCAST,OUT,S0,OFF"]
    for port in range(1, 6):
        must += [f"?BAUD,S{port},9600", f"?BCAST,OUT,S{port},ON", f"?BCAST,IN,S{port},ON"]
    p += [f"after the erase the chain lacks {x}" for x in must if x not in toks]
    left = [redact_token(t) for t in toks if t.upper().startswith(
        ("?LABEL,", "?ALIAS,", "?MAESTRO,", "?WLED,", "?HCR,", "?MP3,", "?DFP,", "?SEQ,", "?VAR,", "?MAP,", "?CONTROLLER,",
         "?WDP,OFF", "?WDP,AUTOJOIN,OFF", "?KYBER,LOCAL"))]
    if left:
        p.append(f"after the erase the chain still holds {left}")
    if token(toks, "?EPASS,") == token(base, "?EPASS,"):
        p.append("the mesh password is unchanged by the erase (expected the firmware default, unless the bench uses it)")
    if token(toks, "?WIFI,") != "?WIFI,OFF":
        p.append("after the erase the WiFi settings are not back to OFF (F8)")
    if any(x.startswith("[WIFI] SoftAP") for x in banner):
        p.append("after the erase the board still brought its access point up (F8)")
    if da_after:
        p.append(f"after the erase {len(da_after)} serial-attached device(s) are still saved (F8)")
    return p


def _erase_cycle(bench, erase_cmd, extras=False):
    """extras (WCB-WP57, nvs.erase_led_and_tails): first ?LED,PIN with W1's own pin and a boot that applies it
    (_led_override); the erase sent with ';S0<t>' chained behind it, which must print before the restart; and, before
    ?HW, the erased board's LED state and ?backup (_erased_board_problems)."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before, Console(bench, 2) as c2:
        base = before[1]
        _replayable(base)
        hw = token(base, "?HW,")
        if hw is None or hw.upper() == "?HW,0":
            raise Skip("W1's chain has no hardware version to restore")
        q = token(base, "?WCBQ,")
        if q is None:
            raise Skip("W1's chain has no ?WCBQ")
        floor = int(q.split(",")[1])
        learned = _learned_peers(w, floor)
        da_before = _da_rows(w)
        bench.note(f"learned peers before the erase: {learned}; saved serial-attached devices, which the erase drops "
                   f"for good (devices re-announce): {sorted(da_before)}")
        erased = False
        try:
            tail = None
            if extras:
                _led_override(w, problems)
                tail = marker("t")
            m = w.send(erase_cmd if tail is None else f"{erase_cmd}^;S0{tail}")
            erased = True
            try:
                w.dev.expect(r"^NVS cleared — set \?HW,xx before use \(serial ports are inactive until then\)\.$",
                             timeout=5, since=m)
                w.dev.expect(r"^NVS: erased \d+ storage area\(s\) - every setting, WiFi, learned peers and stored sequences "
                             r"included\.$",
                             timeout=5, since=m)
                w.dev.expect(r"^Restart queued - restarting once the command queue is quiet$", timeout=5, since=m)
                w.dev.expect(r"^Rebooting now", timeout=15, since=m)
            except AssertionError:
                problems.append(f"{erase_cmd} did not confirm and restart")
            if tail is not None:
                seq = [x.strip() for x in w.dev.since(m)]
                i_t = next((i for i, x in enumerate(seq) if x == tail), None)
                i_r = next((i for i, x in enumerate(seq) if x.startswith("Rebooting now")), None)
                if i_t is None or (i_r is not None and i_r < i_t):
                    problems.append(f"the ;S0 chained behind {erase_cmd} did not run before the restart")
            w.wait_boot(m, timeout=30)
            banner = _cut(w.dev.since(m))
            problems += _defaults_problems(banner, snapshot(bench, 1), base, _da_rows(w), da_before)
            if extras:
                problems += _erased_board_problems(w, banner)
        finally:
            if erased:
                w.run(hw)                        # the pin map first: until a boot on it no serial port is usable
                w.reboot()
                # The erased board runs WDP, and an advert auto-adds its sender's Maestro proxies into the first free
                # slots before the replay reaches its ?MAESTRO lines, so the table came back in another order (run
                # 20260927-174702). WDP off and the table cleared first, as s22 rebuilds one; the chain turns WDP
                # back on only if it was on, since ON is not saved as a token.
                w.run("?WDP,OFF")
                w.run("?MAESTRO,CLEAR,ALL", timeout=8)
                refused = _replay(w, base)
                if "?WDP,OFF" not in base:
                    w.run("?WDP,ON")
                if refused:
                    problems.append("lines refused on the restore: " + "; ".join(refused))
                w.reboot()
                for n in learned:
                    w.run(f"?WDP,ADD,{n}")
                time.sleep(6)                    # LEARNED_FLUSH_DEBOUNCE_MS, then the NVS write
        after = _learned_peers(w, floor)
        if after != learned:
            problems.append(f"learned peers after the restore: {after}, before: {learned}")
        time.sleep(2)
        _mesh_both_ways(w, c2, problems, "after the restore")
    assert not problems, "; ".join(problems)


@test("nvs.erase_defaults_restore", "(attended) ?ERASE,NVS on W1 erases every namespace and restarts; W1 boots on the firmware's defaults (identity, MAC, peers, ports, ETM, WDP, WiFi off, no devices, sequences or saved serial devices); then ?HW and a boot, every line of W1's old chain, a boot and its learned peers put it back exactly, and the mesh works both ways (3 reboots)", needs=["wcb1"], links=[], opt_in="nvs_erase")
def erase_defaults_restore(bench):
    _erase_cycle(bench, "?ERASE,NVS")


@test("nvs.wcb_erase_alias", "(attended) The legacy ?WCB_ERASE is the same erase as ?ERASE,NVS: the same confirmation, the same defaults, the same restore (3 reboots)", needs=["wcb1"], links=[], opt_in="nvs_erase")
def wcb_erase_alias(bench):
    _erase_cycle(bench, "?WCB_ERASE")


# ============================================================ ?NVS (F10)
def _nvs(lines):
    """(stats, {namespace: entries}) from ?NVS output, or (None, {}) when there is no ?NVS line."""
    stats, spaces = None, {}
    for x in lines:
        x = x.rstrip()
        m = re.match(r"^NVS: used=(\d+) free=(\d+) available=(\d+) total=(\d+) namespaces=(\d+) \((\d+)% used\)$", x)
        if m:
            stats = dict(zip(("used", "free", "available", "total", "namespaces", "pct"), (int(v) for v in m.groups())))
            continue
        m = re.match(r"^  (\S+)\s+(\d+)$", x)
        if stats and m:
            spaces[m.group(1)] = int(m.group(2))
    return stats, spaces


def _nvs_problems(stats, spaces, board):
    if not stats:
        return [f"{board}: no 'NVS: used=...' line"]
    p = []
    if stats["used"] + stats["free"] != stats["total"]:
        p.append(f"{board}: used {stats['used']} + free {stats['free']} != total {stats['total']}")
    if stats["available"] > stats["free"]:
        p.append(f"{board}: available {stats['available']} > free {stats['free']}")
    if sum(spaces.values()) + stats["namespaces"] != stats["used"]:
        p.append(f"{board}: the namespaces' entries ({sum(spaces.values())}) plus one per namespace "
                 f"({stats['namespaces']}) do not add up to used ({stats['used']})")
    if list(spaces) != sorted(spaces):
        p.append(f"{board}: the namespaces are not listed alphabetically")
    missing = [n for n in ("wcb_config", "serial_baud", "espnow_config", "hw_version") if n not in spaces]
    if missing:
        p.append(f"{board}: no {missing} in the list")
    return p


@test("wcb.nvs_report", "?NVS on W1 and W2: the usage line and one line per namespace, alphabetical, adding up (used + free = total; the namespaces' entries plus one per namespace = used; available <= free) and naming the settings every board keeps", needs=["wcb1"], links=[])
def nvs_report(bench):
    """printNvsUsage (WCB_Storage.cpp), added 2026-09-24 so a full store can be seen (docs/HIL_TEST_AUDIT.md F10).
    Read-only. W2 is read on its own USB console."""
    w = usb_wcb(bench)
    stats, spaces = _nvs(w.run("?NVS", timeout=6))
    problems = _nvs_problems(stats, spaces, "W1")
    if stats:
        top = sorted(spaces.items(), key=lambda kv: -kv[1])[:4]
        bench.note(f"W1 NVS {stats['used']}/{stats['total']} used, {stats['available']} available; most: {top}")
    with Console(bench, 2) as c2:
        stats2, spaces2 = _nvs(_crun(c2, "?NVS", 2.0))
        problems += _nvs_problems(stats2, spaces2, "W2")
        if stats2:
            bench.note(f"W2 NVS {stats2['used']}/{stats2['total']} used, {stats2['available']} available")
    assert not problems, "; ".join(problems)


def _hil_vars(w):
    """{name: value} of W1's hilnv* variables, from ?VAR,LIST."""
    out = {}
    for x in w.run("?VAR,LIST"):
        m = re.match(r"^\s+(hilnv\w*) = (-?\d+)\s+\[(persistent|volatile)\]", x)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _sequences_into_nvs(w):
    """Reboot W1 once with its stored sequences in NVS - the sequence store's fallback (?DEBUG,SEQNVS,
    WCB_SeqStore.h) - so throwaway sequences fill NVS as they did before the store had a file of its own. Call it
    inside config_guard: W1's own sequences stay in the file, unseen until the next boot, so the guard's snapshot must
    come first. True when W1 was rebooted for it: the caller clears its sequences and reboots once more, before the
    guard compares - that boot moves whatever NVS still lists into the store. False on firmware whose sequences are in
    NVS anyway (no 'Sequences:' line in ?NVS)."""
    if not any(x.startswith("Sequences:") for x in w.run("?NVS", timeout=6)):
        return False
    if not _has(w.run("?DEBUG,SEQNVS"), "The next boot keeps stored sequences in NVS"):
        raise AssertionError("?DEBUG,SEQNVS was not accepted")
    m = w.reboot()
    if not any("Stored sequences are kept in the settings store this boot" in x for x in w.dev.since(m)):
        raise AssertionError("after ?DEBUG,SEQNVS and a reboot W1 did not say it keeps its sequences in NVS")
    return True


@test("seq.nvs_full_consistency", "OPT-IN (nvs_fill): on the sequence store's NVS fallback (?DEBUG,SEQNVS), W1's NVS is filled with throwaway sequences until a save is refused; the refusal names NVS and leaves nothing behind (no listed key, no hidden value), a clear on the full store deletes cleanly, and once all are removed stored_cmds is back to its size before (F9). Then persistent variables until one is refused: no ACK for it, and after a reboot W1 holds what it reported (re-scan #13; 2 reboots)", needs=["wcb1"], links=[], opt_in="nvs_fill")
def nvs_full_consistency(bench):
    """saveStoredCommandsToPreferences / eraseStoredCommandByName (WCB_Storage.cpp). Before 2026-09-24 a failed write
    of the sequence list went unchecked: a clear on a full store left the key listed with no value, and a save could
    leave a value no list names (docs/HIL_TEST_AUDIT.md F9, run 20260924-092602). The values are a ;S0 marker and a
    comment, and nothing recalls them. Other NVS writers on W1 may fail while it is full; that lasts seconds. Stored
    sequences have their own store since 2026-09-29, so the fill runs on its NVS fallback, one boot long
    (_sequences_into_nvs); the reboot that checks the variables takes W1 back."""
    w = usb_wcb(bench)
    before, spaces0 = _nvs(w.run("?NVS", timeout=6))
    if not before:
        raise Skip("W1 has no ?NVS (older firmware)")
    base_seq = spaces0.get("stored_cmds", 0)
    keys, problems, refused = [], [], []
    made, reported = [], None      # the variable arm (re-scan #13)
    setters = None                 # the label and baud arm (re-scan #20): the tokens to put back
    on_nvs = False                 # W1 booted onto the NVS fallback, and must boot back
    with config_guard(bench, 1) as before:
        if _has(w.run("?VAR,SET,hilnvc,7"), "[VAR] hilnvc = 7  [persistent]"):
            made.append("hilnvc")    # a persistent variable from before the fill, cleared on the full store below
        try:
            on_nvs = _sequences_into_nvs(w)
            # Big values fill the store; once one is refused, smaller ones find the last gaps - the tier where a value
            # can still fit but the growing sequence list cannot is the new check. It ends when a 40-character save
            # is refused. Every refused key must leave nothing behind.
            refused, n = [], 0
            for size in (1500, 600, 200, 40):
                while n < 60:
                    n += 1
                    key = f"HILF{n:02d}"
                    head = f";S0{marker('f')}^***"
                    out = w.run(f"?SEQ,SAVE,{key},{head}{'x' * (size - len(head))}", timeout=8)
                    if _has(out, f"Stored: Key='{key}'"):
                        keys.append(key)
                        continue
                    refused.append((key, size, next((x.rstrip() for x in out if "Failed to store sequence" in x), None)))
                    break
            full, spaces_full = _nvs(w.run("?NVS", timeout=6))
            kinds = sorted({("list" if r[2] and "sequence list" in r[2] else "value" if r[2] else "no reason") for r in refused})
            bench.note(f"stored {len(keys)} fill sequence(s); NVS then {full and full['used']}/{full and full['total']}, "
                       f"available {full and full['available']}; refusals {[(r[0], r[1]) for r in refused]}, kinds {kinds}")
            if not refused or refused[-1][1] != 40:
                problems.append("NVS never refused a 40-character save; the store did not fill")
            names = " ".join(w.run("?SEQ,NAMES"))
            # The per-command lines for what is stored, and both one-line chains whole and CRC-valid: a config this big
            # once cost the chains (they printed as a bare checksum, tracker #90).
            backup = w.run("?backup", timeout=15)
            saved = {x.split(",")[2].upper() for x in backup if x.upper().startswith("?SEQ,SAVE,") and x.count(",") >= 3}
            problems += backup_chain_problems(backup, "?backup with the store full")
            for key, size, line in refused:
                if not line:
                    problems.append(f"the refused {size}-character save of {key} did not say why")
                if key in names:
                    problems.append(f"the refused {key} is listed by ?SEQ,NAMES")
                if key.upper() in saved:
                    problems.append(f"the refused {key} is in ?backup")
            missing = [k for k in keys if k.upper() not in saved]
            if missing:
                problems.append(f"stored fill sequence(s) missing from ?backup: {missing}")
            if keys:                                             # a clear while the store is full
                out = w.run(f"?SEQ,CLEAR,{keys[-1]}")
                if not _has(out, f"Deleted stored command key: '{keys[-1]}'"):
                    problems.append(f"clearing {keys[-1]} on the full store printed {out}")
                keys.pop()
            # Variables (WCB-WP42 row 1). Every persistent set rewrites one blob, so they grow it until the store
            # refuses one. A refused set used to print the ERROR and then the '[persistent]' ACK the Wizard waits for,
            # and the value was gone at the next boot; a refused clear said 'Cleared' and the variable came back.
            var_refused = None
            for i in range(1, 61):
                name, value = f"hilnv{i:02d}", 100000000 + i
                out = w.run(f"?VAR,SET,{name},{value}")
                acked = _has(out, f"[VAR] {name} = {value}  [persistent]")
                if _has(out, "[VAR] ERROR"):
                    var_refused = name
                    if acked:
                        problems.append(f"?VAR,SET,{name} printed the ERROR and then the '[persistent]' ACK")
                    if not _has(w.run(f"?VAR,GET,{name}"), f"[VAR] '{name}' is not set"):
                        problems.append(f"the refused {name} is set in RAM all the same")
                    break
                if not acked:
                    problems.append(f"?VAR,SET,{name} printed {out}")
                    break
                made.append(name)
            if "hilnvc" in made:
                out = w.run("?VAR,CLEAR,hilnvc")
                if _has(out, "[VAR] ERROR") and _has(out, "[VAR] Cleared 'hilnvc'"):
                    problems.append("?VAR,CLEAR,hilnvc printed the ERROR and then 'Cleared'")
            bench.note(f"{len(made)} persistent variable(s) set; refused: {var_refused or 'none - the blob still fit'}")
            reported = _hil_vars(w)
            # A label and a baud on the full store (re-scan #20, WCB-WP42 row 3). A refused label is not set - no
            # 'label set to' line, the chain keeps the old one; a refused baud still runs, with a warning, and ?config
            # shows the rate the port runs at: it used to re-read NVS and show the saved one instead.
            setters = [token(before[1], "?LABEL,S5,") or "?LABEL,CLEAR,S5", token(before[1], "?BAUD,S5,") or "?BAUD,S5,9600"]
            out = [x.rstrip() for x in w.run("?LABEL,S5,HILFULL")]
            label_stored = _has(out, "Serial5 label set to")
            if "⚠️  NVS could not store the Serial5 label (full? see ?NVS) - label unchanged" in out:
                if _has(out, "Serial5 label set to"):
                    problems.append("a refused label was reported set")
                if token(snapshot(bench, 1), "?LABEL,S5,") != token(before[1], "?LABEL,S5,"):
                    problems.append("a refused label changed the chain")
            elif "Serial5 label set to: 'HILFULL'" not in out:
                problems.append(f"?LABEL,S5,HILFULL on the full store printed {out}")
            out = [x.rstrip() for x in w.run("?BAUD,S5,19200")]
            if "Baud rate for Serial5 updated to 19200" not in out:
                problems.append(f"?BAUD,S5,19200 on the full store printed {out}")
            baud_refused = any(x.startswith("⚠️  NVS could not store it") for x in out)
            if not any(re.search(r"Serial5.*19200", x) for x in w.run("?config", timeout=6)):
                problems.append("?config does not show S5 at the 19200 it runs at")
            bench.note(f"full store: label {'stored' if label_stored else 'refused'}, "
                       f"baud {'refused (runs, warned)' if baud_refused else 'stored'}")
        finally:
            for k in keys:
                w.run(f"?SEQ,CLEAR,{k}")
            for key, _, _ in refused:
                w.run(f"?SEQ,CLEAR,{key}")                      # harmless when it was never stored
            for cmd in setters or []:                           # after the clears: the store has room again
                w.run(cmd)
            try:
                if reported is not None or on_nvs:               # the store has room again: what boots is NVS
                    w.reboot()                                   # (and a fallback boot moves back to the file)
                if reported is not None:
                    booted = _hil_vars(w)
                    if booted != reported:
                        problems.append(f"after a reboot W1 holds variables {booted}, but reported {reported}: a set or "
                                        f"clear it acknowledged did not reach NVS, or one it refused did")
            finally:
                for name in made:
                    w.run(f"?VAR,CLEAR,{name}")
        left = [t for t in snapshot(bench, 1) if re.match(r"^\?SEQ,SAVE,HILF\d\d,", t, re.I)]
        if left:
            problems.append(f"fill sequences left in ?backup: {left}")
        after, spaces1 = _nvs(w.run("?NVS", timeout=6))
        if after and spaces1.get("stored_cmds", 0) != base_seq:
            problems.append(f"stored_cmds used {base_seq} entries before and {spaces1.get('stored_cmds', 0)} after: "
                            f"a value was left that no list names")
    assert not problems, "; ".join(problems)


# ============================================================ one-line restore through the ?CHK gate (WCB-WP12)
VERIFIED_RESTORE = "Command checksum VERIFIED - Configuration is intact"    # parseCommandsAndEnqueue (WCB.ino)
FAILED_RESTORE = "Command checksum FAILED - Configuration may be corrupted!"
ABORTED_RESTORE = "Aborting restore to prevent corruption."


def _run_chain(w, text, timeout=30):
    """w.run for a line that carries W1's credentials. The replies echo them ('ESP-NOW password updated to: <pw>', the
    ?EPASS handler in WCB.ino), which hil/checkpoint.py's redaction does not recognise, so a timeout is raised again
    without the device's last lines."""
    try:
        return w.run(text, timeout=timeout)
    except AssertionError:
        raise AssertionError(f"W1 did not finish a {len(text)}-character line within {timeout} s") from None


def _overflows(w):
    """The USB RX-overflow count W1's ?STATS reports; it prints no line while the count is 0 (buildStatsString)."""
    for x in w.run("?STATS", timeout=6):
        m = re.match(r"^USB/S0 input: (\d+) RX overflow", x)
        if m:
            return int(m.group(1))
    return 0


def _label_value(base, want):
    """base + the first counter for which the CRC-32 of '?LABEL,S5,<value>' satisfies want(crc)."""
    return next(f"{base}{k}" for k in range(4000) if want(chain_crc(f"?LABEL,S5,{base}{k}")))


@test("backup.one_line_restore", "W1's configured chain pasted back as ONE line goes through the ?CHK gate: 'Command checksum VERIFIED - Configuration is intact', no line refused, no USB RX overflow, the chain unchanged; one flipped byte fails the checksum and applies nothing ('Aborting restore'); a lowercase or zero-stripped CRC verifies; the last ^?CHK is the checksum and an earlier one is a token; a 250-token chain, past the 200-slot command queue, runs every token (WCB-WP12, re-scan #24)", needs=["wcb1"], links=[])
def one_line_restore(bench):
    """parseCommandsAndEnqueue (WCB.ino) finds the checksum with lastIndexOf (the live function identifier's CHK
    first, then '?CHK'), bounds it at the next delimiter, upper-cases it and left-pads it to 8 digits (6.0.x and
    older wrote it unpadded); on a match it hands the text before it to parseCommandsNoChecksum, on a mismatch it
    prints the FAILED lines and runs nothing. A USB line is split on serialCommandTask, whose enqueue waits up to 2 s
    for room (enqueueCommand), so a chain longer than the 200-slot queue is no longer cut (re-scan #24: the plan's
    '(f) characterise the queue-full lines' predates that fix). The chain carries ?EPASS and ?WIFI: it goes back to
    W1 only and is never quoted. The S5 label the arms set is put back."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        _replayable(before[1])
        restore = token(before[1], "?LABEL,S5,") or "?LABEL,CLEAR,S5"
        line = backup_chain_lines(w.run("?backup", timeout=10))["configured"]
        if not line or not CHK_LINE.match(line):
            raise AssertionError(f"W1's ?backup has no checksummed configured chain ({len(line or '')} chars)")
        try:
            # (a) the whole configured chain, one line
            over = _overflows(w)
            out = _run_chain(w, line)
            if not _has(out, VERIFIED_RESTORE):
                problems.append(f"W1's own {len(line)}-character chain, sent as one line, did not verify")
            bad = [redact_text(x.strip()) for x in out if _REFUSED.search(x)]
            if bad:
                problems.append(f"{len(bad)} line(s) refused on the one-line restore: {bad[:3]}")
            if _overflows(w) > over:
                problems.append("the one-line restore overflowed W1's USB receive buffer")
            if snapshot(bench, 1) != before[1]:
                problems.append("the one-line restore changed W1's chain")
            # (b) one byte flipped: the whole chain plus an S5 label, the label's last character changed after the CRC
            chain = f"{line[:line.rfind('^?CHK')]}^?LABEL,S5,{marker('R')}"
            flipped = chain[:-1] + ("0" if chain[-1] != "0" else "1")
            out = _run_chain(w, f"{flipped}^?CHK{chain_crc(chain)}")
            if not (_has(out, FAILED_RESTORE) and _has(out, ABORTED_RESTORE)):
                problems.append("a chain with one byte flipped did not fail its checksum and abort")
            if _has(out, "Command checksum VERIFIED") or _has(out, "label set to"):
                problems.append("a chain with one byte flipped was applied")
            if token(snapshot(bench, 1), "?LABEL,S5,") != token(before[1], "?LABEL,S5,"):
                problems.append("a chain with one byte flipped changed S5's label")
            # (c) the CRC in lower case, then without its leading zeros
            base = marker("C")
            for how, value, spell in (
                    ("in lower case", _label_value(base, lambda c: re.search("[A-F]", c)), str.lower),
                    ("with its leading zeros stripped", _label_value(base, lambda c: c.startswith("0")),
                     lambda c: c.lstrip("0"))):
                chain = f"?LABEL,S5,{value}"
                out = w.run(f"{chain}^?CHK{spell(chain_crc(chain))}")
                if not _has(out, VERIFIED_RESTORE) or not _has(out, f"Serial5 label set to: '{value}'"):
                    problems.append(f"a chain whose CRC is written {how} did not verify and apply")
            # (e) an earlier ^?CHK inside the chain is a token, not the checksum
            value = marker("X")
            chain = f"?LABEL,S5,{value}^?CHK00000000^?VERSION"
            out = w.run(f"{chain}^?CHK{chain_crc(chain)}")
            if not _has(out, VERIFIED_RESTORE):
                problems.append("a chain holding an earlier ^?CHK token did not verify against its last one")
            elif not (_has(out, f"Serial5 label set to: '{value}'") and _has(out, "Software Version:")):
                problems.append("a chain holding an earlier ^?CHK token verified but did not run both commands")
            # (f) 250 tokens, past the 200-slot command queue
            base = marker("q")
            want = [f"{base}{k:03d}" for k in range(250)]
            chain = "^".join(f";S0{x}" for x in want)
            out = w.run(f"{chain}^?CHK{chain_crc(chain)}", timeout=40)
            got = [x.strip() for x in out if x.strip().startswith(base)]
            full = sum("Command queue is full" in x for x in out)
            bench.note(f"one-line restore: chain {len(line)} characters; 250-token chain: {len(got)} run, {full} "
                       f"'queue is full' line(s)")
            if got != want or full:
                problems.append(f"a 250-token chain ran {len(got)} of its tokens"
                                + ("" if got == want[:len(got)] else " out of order")
                                + f", with {full} 'Command queue is full' line(s)")
        finally:
            w.run(restore)
    assert not problems, "; ".join(problems)


def _grouped(tokens):
    """Chain tokens with each sequence value's '^' parts joined back: a part never starts with a function identifier,
    and the factory chain changes identifier part-way."""
    out = []
    for t in tokens:
        if out and t[:1] not in ("?", "!"):
            out[-1] += "^" + t
        else:
            out.append(t)
    return out


def _funcchar_chain_problems(configured, factory, seq):
    """Under ?FUNCCHAR,!: the configured chain is '!' throughout and ends ^!CHK; the factory chain keeps '?' up to its
    ?FUNCCHAR,! token and '!' after it, ends ^?CHK, and holds the stored `seq` token whole; both CRC-valid. Token names
    only in the messages."""
    p = []
    for name, text, lfi in (("configured", configured, "!"), ("factory", factory, "?")):
        m = re.match(rf"^(.*)\^{re.escape(lfi)}CHK([0-9A-F]{{8}})$", text or "")
        if not m:
            p.append(f"the {name} chain ({len(text or '')} chars) does not end in ^{lfi}CHK<8 hex>")
            continue
        if chain_crc(m.group(1)) != m.group(2):
            p.append(f"the {name} chain fails its CRC")
        toks = _grouped(m.group(1).split("^"))
        if name == "configured":
            off = [t.split(",")[0] for t in toks if not t.startswith("!")]
        else:
            i = next((k for k, t in enumerate(toks) if t == "?FUNCCHAR,!"), len(toks))
            if i == len(toks):
                p.append("the factory chain has no ?FUNCCHAR,! token")
            off = [t.split(",")[0] for t in toks[:i] if not t.startswith("?")]
            off += [t.split(",")[0] for t in toks[i + 1:] if not t.startswith("!")]
        if off:
            p.append(f"the {name} chain has {len(off)} token(s) with the wrong identifier: {off[:4]}")
        if seq not in toks:
            p.append(f"the {name} chain does not hold the stored HILFR sequence whole")
    return p


def _seqval(w, key):
    """The value !SEQ,GET,<key> prints ([MGMT:SEQVAL,<n>]<key>,OK,<value>), or None."""
    m = next((re.match(rf"^\[MGMT:SEQVAL,\d+\]{key},OK,(.*)$", x.rstrip()) for x in w.run(f"!SEQ,GET,{key}")
              if x.startswith("[MGMT:SEQVAL,")), None)
    return m.group(1) if m else None


@test("backup.chain_restore_funcchar", "Under ?FUNCCHAR,! the configured chain is '!' throughout and ends ^!CHK, the factory chain keeps '?' up to ?FUNCCHAR,! and '!' after it, both CRC-valid; each verifies as one line - the configured one on the '!' board, the factory one on a '?' board, which it flips to '!' - and keeps a stored '!SEQ,SAVE,HILFR,;S1a^;S1b' whole; the ?C-first branch takes '!CHK', falls back to '^?CHK', bounds a checksum a command follows and runs only what it covers; nothing reaches S1 (WCB-WP12)", needs=["wcb1"])
def chain_restore_funcchar(bench):
    """printBackupConfig (WCB.ino) writes the configured chain with the live identifier and <lfi>CHK, the factory
    chain with '?' until its ?FUNCCHAR token and the new identifier after it, checksum ^?CHK. parseCommandsAndEnqueue
    takes either checksum spelling, and parseCommandsNoChecksum follows the chain's own identifier flip (curFunc), so a
    !SEQ,SAVE value after it is not split at its '^'. The ?C-first branch of processSerialCommandHelper verifies a
    legacy ?CS store on its own - '<lfi>CHK' first, then '?CHK', bounded at the next delimiter - and hands on only the
    text before the checksum, so a command after it is dropped (the plan row expected it to run; this follows the
    code). While the identifier is '!' nothing goes out with '?', which would be broadcast as text; WCB.run's ';S0'
    echo is not affected. The ?DELIM half of plan row 3 is left to chars.delim_change_restore (s18)."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    problems = []
    a, b, c, d, e, f = (marker(x) for x in "abcdef")
    seq = f"!SEQ,SAVE,HILFR,;S1{a}^;S1{b}"
    keys = ("HILFR", "HILCK", "HILCK2", "HILCK3")
    with config_guard(bench, 1) as before:
        _replayable(before[1])
        pm = s1.mark()
        try:
            _sent(w, "?FUNCCHAR,!", r"^Local function identifier updated to '!'")
            _sent(w, seq, r"^Stored: Key='HILFR'")
            m = w.dev.mark()
            w.dev.send("WCB_WEBTOOL_CONFIG_PULL")
            w.dev.expect(r"For Factory Reset/Fresh Boards", timeout=5, since=m)
            time.sleep(1.5)
            configured, factory = _chains(w.dev.since(m))
            problems += _funcchar_chain_problems(configured, factory, seq)
            out = _run_chain(w, configured)
            if not _has(out, VERIFIED_RESTORE):
                problems.append("the '!' configured chain did not verify as one line on the '!' board")
            if _seqval(w, "HILFR") != seq.split(",", 3)[3]:
                problems.append("after the configured chain, !SEQ,GET,HILFR is not the stored value")
            for key, value, chk, tail in (("HILCK", f";S1{c}", "!", ""), ("HILCK2", f";S1{d}", "?", ""),
                                          ("HILCK3", f";S1{e}", "?", f"^;S0{f}")):
                body = f"!CS{key},{value}"
                out = w.run(f"{body}^{chk}CHK{chain_crc(body)}{tail}")
                what = f"!CS{key} with ^{chk}CHK" + (" and a command after it" if tail else "")
                if not _has(out, "Command checksum VERIFIED"):
                    problems.append(f"{what} did not verify")
                elif not _has(out, f"Stored: Key='{key}', Value='{value}'"):
                    problems.append(f"{what} verified but did not store its value")
                if tail and any(x.strip() == f for x in out):
                    problems.append(f"{what}: the command after the checksum ran, outside what the checksum covers")
            _sent(w, "!FUNCCHAR,?", r"^Local function identifier updated to '\?'")
            out = _run_chain(w, factory)
            if not _has(out, VERIFIED_RESTORE):
                problems.append("the factory chain did not verify as one line on the '?' board")
            if not _has(out, "Local function identifier updated to '!'"):
                problems.append("the factory chain did not flip the board to '!'")
            if _seqval(w, "HILFR") != seq.split(",", 3)[3]:
                problems.append("after the factory chain, !SEQ,GET,HILFR is not the stored value")
        finally:
            _, lfi = _live_chars(w)
            if lfi != "?":
                w.dev.send(f"{lfi}FUNCCHAR,?")
                time.sleep(0.5)
            for k in keys:
                w.run(f"?SEQ,CLEAR,{k}")
        heard = s1.received(pm)
        ran = [x for x in (a, b, c, d, e) if x.encode() in heard]
        if ran:
            problems.append(f"{len(ran)} stored ;S1 value(s) reached W1 S1: a store ran its value")
    assert not problems, "; ".join(problems)


# ============================================================ a full store: mappings and the device list (WCB-WP42)
TEN = {"S2": ",".join(f"W{n}S{p}" for n in (3, 4) for p in range(1, 6)),        # boards the bench does not have
       "S3": ",".join(f"W{n}S{p}" for n in (5, 6) for p in range(1, 6)),
       "S4": ",".join(f"W{n}S{p}" for n in (7, 8) for p in range(1, 6))}
MAP_REFUSED = "Serial mapping {}: NVS could not store it (full?) - it will not survive a reboot. See ?NVS."
DA_REFUSED = "[WDP-DA] could not save the device list to NVS (full?) - retrying in 60 s"


def _fill_store(w, keys, refused):
    """seq.nvs_full_consistency's fill: throwaway HILF<nn> sequences, big then small, until a 40-character one is
    refused -> True when it was."""
    n, size_refused = 0, None
    for size in (1500, 600, 200, 40):
        while n < 60:
            n += 1
            key = f"HILF{n:02d}"
            head = f";S0{marker('f')}^***"
            out = w.run(f"?SEQ,SAVE,{key},{head}{'x' * (size - len(head))}", timeout=8)
            if _has(out, f"Stored: Key='{key}'"):
                keys.append(key)
                continue
            refused.append(key)
            size_refused = size
            break
    return size_refused == 40


def _announce_device(s5, w, name):
    """A HIL device announces itself twice on W1 S5; the second announce confirms it ('saved') and schedules the list
    save 1 s later (wdpDaMarkDirty) -> the host time of the save's refusal line, or None if the save went through."""
    line = f'@WDP1 {{"type":"{name}","fw":"1"}}\n'.encode()
    prime(s5)
    time.sleep(0.3)
    m = w.dev.mark()
    s5.send(line)
    time.sleep(1.0)
    s5.send(line)
    w.dev.expect(rf"^\[WDP-DA\] S5: {name} saved$", timeout=3, since=m)
    time.sleep(2.5)
    return _at(w.dev, m, "^" + re.escape(DA_REFUSED))[0]


@test("nvs.full_map_and_device_save", "OPT-IN (nvs_fill): on a full NVS a serial mapping whose entry count cannot be stored says 'NVS could not store it', and after a reboot every mapping is exactly as last stored - W1 S2 back on its ten destinations with no S0, a new mapping that could not be stored gone, never a mix; a WDP-DA device list that cannot be saved says so, is saved within 60 s of there being room, and a reboot reloads the device (re-scan F20; WCB-WP42 rows 2 and 4; 1 reboot)", needs=["wcb1"], links=[], opt_in="nvs_fill")
def full_map_and_device_save(bench):
    """saveSerialMonitorMappings (WCB_Storage.cpp) checks each slot's sm<i>_cnt write, warns, and then keeps the output
    keys past the new count (removeUnusedSerialMapKeys), so an old count never loads with outputs missing (as S0); it
    writes _act last, so a slot it could not write loads inactive. wdpDaSave (WCB_WDP.cpp) checks its putBytes and
    retries 60 s later. The plan re-issues S2 right after the sequence fill, but a store that refuses a 40-character
    value can still hold an entry or two, and a one-entry count rewrite then succeeds with no warning; so two new
    ten-destination mappings (about 26 new one-entry keys each) take the last entries first, on S3 and S4 with their
    broadcast flags set beforehand so a mapping writes nothing else. Whichever writes the store refused, the reboot
    must bring each mapping back exactly as issued or as last stored and warned about. When no refusal happens at all,
    nothing was tested and the test skips. Everything is put back; the device is forgotten. The sequences fill NVS on
    the sequence store's NVS fallback, one boot long (_sequences_into_nvs); the reboot the test takes goes back."""
    w = usb_wcb(bench)
    s5 = bench.links.usable(1, "S5", send=True)
    stats0, spaces0 = _nvs(w.run("?NVS", timeout=6))
    if not stats0:
        raise Skip("W1 has no ?NVS (older firmware)")
    keys, refused, problems, warned = [], [], [], {}
    name, da_refused = "HILNF" + nonce()[:4], None
    on_nvs = rebooted = False
    with config_guard(bench, 1) as before:
        for p in TEN:
            if token(before[1], f"?MAP,SERIAL,{p},"):
                raise Skip(f"W1 already maps {p}")
        flags = [t for t in before[1] if re.match(r"^\?BCAST,(IN|OUT),S[34],", t)]
        if s5 is not None:
            _da_scrub(w, "S5")
        try:
            on_nvs = _sequences_into_nvs(w)
            for p in ("S3", "S4"):
                w.run(f"?BCAST,IN,{p},OFF")
                w.run(f"?BCAST,OUT,{p},OFF")
            if not _has(w.run(f"?MAP,SERIAL,S2,{TEN['S2']}"), "Serial mapping set: Serial2 -> 10 destination(s)"):
                raise AssertionError("?MAP,SERIAL,S2 with ten destinations did not confirm")
            if not _fill_store(w, keys, refused):
                problems.append("NVS never refused a 40-character save; the store did not fill")
            for p in ("S3", "S4"):
                warned[p] = _has(w.run(f"?MAP,SERIAL,{p},{TEN[p]}"), MAP_REFUSED.format(p))
                if warned[p]:
                    break
            warned["S2"] = _has(w.run("?MAP,SERIAL,S2,S4"), MAP_REFUSED.format("S2"))
            mda = w.dev.mark()
            if s5 is not None:
                da_refused = _announce_device(s5, w, name)
            for k in keys + refused:                         # room again
                w.run(f"?SEQ,CLEAR,{k}")
            keys, refused = [], []
            if da_refused is not None:
                time.sleep(max(0.0, da_refused + 62 - time.monotonic()))
                if sum(x.startswith(DA_REFUSED) for x in w.dev.since(mda)) > 1:
                    problems.append("the WDP-DA list save was refused again 60 s later, with room in the store")
            w.reboot()
            rebooted = True
            after = snapshot(bench, 1)
            want = f"?MAP,SERIAL,S2,{TEN['S2']}" if warned["S2"] else "?MAP,SERIAL,S2,S4"
            got = token(after, "?MAP,SERIAL,S2,")
            if got != want:
                problems.append(f"after the reboot S2 maps to {got[15:] if got else 'nothing'}, not "
                                f"{want[15:]} (its re-issue was {'refused' if warned['S2'] else 'stored'})")
            for p in (x for x in ("S3", "S4") if x in warned):
                got = token(after, f"?MAP,SERIAL,{p},")
                if got is not None and (warned[p] or got != f"?MAP,SERIAL,{p},{TEN[p]}"):
                    problems.append(f"after the reboot {p} maps to {got[15:]}, though its mapping was "
                                    + ("refused ('could not store it')" if warned[p] else "issued with ten destinations"))
            if s5 is not None and name not in _da_types(w, "S5"):
                problems.append("the WDP-DA device was not reloaded after the reboot"
                                + (" (its save was refused, then due again 60 s later)" if da_refused else ""))
            bench.note(f"full store: mapping refusals {warned}; WDP-DA list save "
                       + ("not tried (no usable W1 S5 wire)" if s5 is None
                          else "refused, then retried" if da_refused else "went through"))
        finally:
            for k in keys + refused:
                w.run(f"?SEQ,CLEAR,{k}")
            for p in TEN:
                w.run(f"?MAP,SERIAL,CLEAR,{p}")
            for t in flags:
                w.run(t)
            if s5 is not None:
                _da_forget(w, "S5", name)
            if on_nvs and not rebooted:                      # back to the sequence store before the guard compares
                w.reboot()
    stats1, spaces1 = _nvs(w.run("?NVS", timeout=6))
    if stats1 and spaces1.get("serial_map", 0) != spaces0.get("serial_map", 0):
        problems.append(f"serial_map used {spaces0.get('serial_map', 0)} entries before and "
                        f"{spaces1.get('serial_map', 0)} after: keys of a refused mapping were left behind")
    assert not problems, "; ".join(problems)
    if not warned.get("S2") and not any(warned.get(p) for p in ("S3", "S4")) and da_refused is None:
        raise Skip("the store took every mapping and device-list write, so no refusal path ran (the mappings and the "
                   "device still came back right)")


# ============================================================ the mesh-password window (WCB-WP43)
@test("ident.epass_window_gates", "(attended) With a 39-character throwaway mesh password on W1 (the longest ?EPASS takes) for about 4 s, W2 ignores W1's config pull, sequence-names request and OTA BEGIN (no 'Config request', 'Sequence-names request' or 'BEGIN rejected' line), and W1 prints no reply and none of W2's remote-terminal lines; with W1's own password back from its chain each answers again (WCB-WP43)", needs=["wcb1", "wcb2"], links=[], opt_in="mesh_password")
def epass_window_gates(bench):
    """Each receiver compares the packet's 40-byte password field with its own before anything else:
    handleConfigReqPacket and handleSeqReqPacket (WCB.ino) before they log, otaPktAuth (WCB_OTA.cpp) before BEGIN
    reaches otaBegin's brick guard, rtermRelayHandlePacket (WCB_RemoteTerm.cpp) on the relay. The ?EPASS handler
    (WCB.ino) takes a password shorter than that field, so 39 characters is the longest. The BEGIN names the chip
    family W2 is not (?OTALOCAL,STATUS), so even a gate that failed open would stop at the brick guard, before any
    erase - the control after the restore shows it does. W1's own ?EPASS line is read from its chain and handed back
    to W1 only; the throwaway is the only password any message names."""
    w1, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    problems = []
    fam = next((re.search(r"\(family (\d+)\)", x) for x in w2.run("?OTALOCAL,STATUS") if x.startswith("Chip:")), None)
    if not fam or fam.group(1) not in ("0", "1"):
        raise Skip("W2's chip family is unknown (?OTALOCAL,STATUS)")
    other = 1 - int(fam.group(1))
    sess = int(nonce(), 16) % 60000 + 1
    with config_guard(bench, 1) as before:
        own = token(before[1], "?EPASS,")
        if own is None:
            raise Skip("W1's chain has no ?EPASS line to restore")
        try:
            w2.run("?DEBUG,MGMT,ON")
            with w1.remote_terminal(2, bench.usb_wcb_number()):
                changed = False
                try:
                    tmp = padded("pw", 39)                    # a throwaway, never a real credential
                    out = w1.run(f"?EPASS,{tmp}")
                    changed = True
                    if not _has(out, f"ESP-NOW password updated to: {tmp}"):
                        problems.append("a 39-character ?EPASS did not confirm")
                    m1, m2 = w1.dev.mark(), w2.dev.mark()
                    for cmd in ("?MGMT,PULL,2", "?MGMT,SEQ,2", f"?OTA,BEGIN,2,{sess},4096,{other}"):
                        w1.dev.send(cmd)
                        time.sleep(0.2)
                    w2.dev.send("?VERSION")                   # mirrored to W1 only if W1 takes W2's password
                    time.sleep(3.0)
                finally:
                    if changed and not _has(_run_chain(w1, own, timeout=5), "ESP-NOW password updated to: "):
                        problems.append("replaying W1's own ?EPASS line did not confirm")
                if changed:
                    heard = [x for x in w2.dev.since(m2) if re.match(
                        r"^\[MGMT\] (Config request|Duplicate config request|Sequence-names request|Duplicate seq "
                        r"request) from WCB1|^\[OTA\] BEGIN rejected", x)]
                    if heard:
                        problems.append(f"with a wrong password W2 still took {len(heard)} request(s): "
                                        f"{[x[:40] for x in heard]}")
                    got = [x[:24] for x in w1.dev.since(m1) if parse_pull_line(x) or x.startswith(
                        ("[MGMT:SEQ,2]", f"[OTA:ACK,2,{sess},", "[TERM:2]"))]
                    if got:
                        problems.append(f"with a wrong password W1 still printed {got}")
                time.sleep(1.0)
                mc = w1.dev.mark()
                w2.dev.send("?VERSION")
                try:
                    w1.dev.expect(r"^\[TERM:2\]Software Version:", timeout=4, since=mc)
                except AssertionError:
                    problems.append("with W1's own password back, W2's remote-terminal lines still do not reach it")
            w1.pull_reply(2, timeout=10, verify=False)
            mc = w1.dev.mark()
            w1.dev.send("?MGMT,SEQ,2")
            try:
                w1.dev.expect(r"^\[MGMT:SEQ,2\]", timeout=5, since=mc)
            except AssertionError:
                problems.append("with W1's own password back, ?MGMT,SEQ,2 is still unanswered")
            mc, mw = w1.dev.mark(), w2.dev.mark()
            w1.dev.send(f"?OTA,BEGIN,2,{sess + 1},4096,{other}")
            try:
                w2.dev.expect(rf"^\[OTA\] BEGIN rejected: image chip family {other} != this board {fam.group(1)} "
                              rf"\(brick guard\)", timeout=5, since=mw)
                w1.dev.expect(rf"^\[OTA:ACK,2,{sess + 1},\d+,1\]", timeout=5, since=mc)
            except AssertionError:
                problems.append("with W1's own password back, W2 did not refuse the wrong-family BEGIN and say so")
        finally:
            w2.run("?DEBUG,MGMT,OFF")
    assert not problems, "; ".join(problems)


# ============================================================ the LED pin and the erase's tails (WCB-WP57)
def _led_override(w, problems):
    """?LED,PIN,<the pin ?LED reports>: saved to NVS (led_config) and applied again at every boot, until an erase
    removes it (saveStatusLEDPin / loadStatusLEDPin, WCB_Storage.cpp). ?backup emits ?LED,PIN only on HW 3.x, so
    config_guard cannot see it on W1; the same pin changes nothing else. One reboot shows the boot applying it."""
    m = next((re.match(r"^LED pin: GPIO(\d+)$", x.rstrip()) for x in w.run("?LED") if x.startswith("LED pin: GPIO")), None)
    if not m:
        problems.append("?LED did not report the LED pin")
        return
    pin = m.group(1)
    out = [x.rstrip() for x in w.run(f"?LED,PIN,{pin}", timeout=8)]
    for want in (f"LED pin GPIO{pin} saved to NVS.", f"LED pin changed to GPIO{pin}. Reinitializing LED..."):
        if want not in out:
            problems.append(f"?LED,PIN,{pin} did not print {want!r}")
    if "led_config" not in _nvs(w.run("?NVS", timeout=6))[1]:
        problems.append("?NVS lists no led_config after ?LED,PIN")
    mb = w.reboot()
    if not any(x.rstrip() == f"LED pin override from NVS: GPIO{pin}" for x in w.dev.since(mb)):
        problems.append(f"the boot after ?LED,PIN,{pin} did not print 'LED pin override from NVS: GPIO{pin}'")


def _erased_board_problems(w, banner):
    """Before ?HW on the erased board: no LED override left, ?backup warns that no hardware version is set (outside the
    chains, printBackupConfig) and both chains are CRC-valid and start with ?HW,0."""
    p = []
    if any(x.startswith("LED pin override from NVS") for x in banner):
        p.append("after the erase the boot still applied an LED pin override")
    if "led_config" in _nvs(w.run("?NVS", timeout=6))[1]:
        p.append("after the erase ?NVS still lists led_config")
    lines = w.run("?backup", timeout=10)
    if not _has(lines, "WARNING: Hardware version not set! Use ?HW,xx to set version before backup."):
        p.append("the erased board's ?backup does not warn that no hardware version is set")
    p += backup_chain_problems(lines, "the erased board's ?backup")
    p += [f"the erased board's {name} chain does not start with ?HW,0" for name, line in
          backup_chain_lines(lines).items() if line and not line.startswith("?HW,0^")]
    return p


@test("nvs.erase_led_and_tails", "(attended) W1's own LED pin set with ?LED,PIN is saved (led_config) and applied again at boot until ?ERASE,NVS removes it; a command chained behind ?ERASE,NVS still runs before the restart; the erased board's ?backup warns that no hardware version is set and both chains are CRC-valid from ?HW,0; then the same restore as nvs.erase_defaults_restore (4 reboots; WCB-WP57)", needs=["wcb1"], links=[], opt_in="nvs_erase")
def erase_led_and_tails(bench):
    """eraseNVSFlash (WCB_Storage.cpp) clears led_config with the rest and defers its restart through rebootPending
    (CLAUDE.md rule 11), so '?ERASE,NVS^;S0<t>' prints <t> before 'Rebooting now'. The erase, the defaults check and
    the restore are _erase_cycle's, the replay with WDP off included."""
    _erase_cycle(bench, "?ERASE,NVS", extras=True)
