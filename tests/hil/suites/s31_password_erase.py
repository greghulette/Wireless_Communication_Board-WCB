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
  nothing is left half-saved or listed empty (F9), then removes them.

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
from suites.common import Console, config_guard, marker, snapshot, token, usb_wcb
from suites.s27_identity_destructive import _learned_peers
from suites.s03_wcb import backup_chain_problems
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
    """Send each chain line on its own, in chain order (?CHK and the read-only ?PEERSLIVE left out). Returns the
    refused lines, redacted, so the caller can report them without quoting a credential."""
    bad = []
    for t in tokens:
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


def _erase_cycle(bench, erase_cmd):
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
            m = w.send(erase_cmd)
            erased = True
            try:
                w.dev.expect(r"^NVS cleared — set \?HW,xx before use \(serial ports are inactive until then\)\.$",
                             timeout=5, since=m)
                w.dev.expect(r"^NVS: erased \d+ storage area\(s\) - every setting, WiFi and learned peers included\.$",
                             timeout=5, since=m)
                w.dev.expect(r"^Restart queued - restarting once the command queue is quiet$", timeout=5, since=m)
                w.dev.expect(r"^Rebooting now", timeout=15, since=m)
            except AssertionError:
                problems.append(f"{erase_cmd} did not confirm and restart")
            w.wait_boot(m, timeout=30)
            banner = _cut(w.dev.since(m))
            problems += _defaults_problems(banner, snapshot(bench, 1), base, _da_rows(w), da_before)
        finally:
            if erased:
                w.run(hw)                        # the pin map first: until a boot on it no serial port is usable
                w.reboot()
                refused = _replay(w, base)
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


@test("seq.nvs_full_consistency", "OPT-IN (nvs_fill): W1's NVS is filled with throwaway sequences until a save is refused; the refusal names NVS and leaves nothing behind (no listed key, no hidden value), a clear on the full store deletes cleanly, and once all are removed stored_cmds is back to its size before (F9)", needs=["wcb1"], links=[], opt_in="nvs_fill")
def nvs_full_consistency(bench):
    """saveStoredCommandsToPreferences / eraseStoredCommandByName (WCB_Storage.cpp). Before 2026-09-24 a failed write
    of the sequence list went unchecked: a clear on a full store left the key listed with no value, and a save could
    leave a value no list names (docs/HIL_TEST_AUDIT.md F9, run 20260924-092602). The values are a ;S0 marker and a
    comment, and nothing recalls them. Other NVS writers on W1 may fail while it is full; that lasts seconds."""
    w = usb_wcb(bench)
    before, spaces0 = _nvs(w.run("?NVS", timeout=6))
    if not before:
        raise Skip("W1 has no ?NVS (older firmware)")
    base_seq = spaces0.get("stored_cmds", 0)
    keys, problems, refused = [], [], []
    with config_guard(bench, 1):
        try:
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
        finally:
            for k in keys:
                w.run(f"?SEQ,CLEAR,{k}")
            for key, _, _ in refused:
                w.run(f"?SEQ,CLEAR,{key}")                      # harmless when it was never stored
        left = [t for t in snapshot(bench, 1) if re.match(r"^\?SEQ,SAVE,HILF\d\d,", t, re.I)]
        if left:
            problems.append(f"fill sequences left in ?backup: {left}")
        after, spaces1 = _nvs(w.run("?NVS", timeout=6))
        if after and spaces1.get("stored_cmds", 0) != base_seq:
            problems.append(f"stored_cmds used {base_seq} entries before and {spaces1.get('stored_cmds', 0)} after: "
                            f"a value was left that no list names")
    assert not problems, "; ".join(problems)
