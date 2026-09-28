"""WCB console basics — config integrity and command parsing, and the mesh config pull at size (F13) and its target-side
scheduler (WCB-WP25). Changes nothing, except the size and pull tests, which add throwaway sequences (HILB../HILP..) or
arm a RAM-only ?DEBUG knob on W2 and remove them again; wcb.pull_holds_restart reboots W2, wcb.pull_changed_restart
changes W2's S5 label, wcb.chain_partial_tokens W1's controller id, and wcb.pull_session_reaped W1's third MAC octet,
each for seconds and put back."""
import math
import re
import time

from hil.runner import Skip, test
from hil.wcb import (CONFIG_TEXT, PULL_MAX, WCB, Pull, PullCollector, PullRefused, chain_crc, comparable, dev_note,
                     group_tokens, parse_error, parse_part, parse_pull_line, screen_code)
from suites.common import config_guard, marker, remote_wcbs, token, usb_wcb

CHK_LINE = re.compile(r"^(.*)\^[?]CHK([0-9A-Fa-f]{8})$")
# PULL_MAX (hil/wcb.py) is MGMT_MAX_CHUNKS x (CONFIG_PAYLOAD_SIZE - 1) = 2912: the most one relay session carries. A
# reply up to it is one [MGMT:CONFIG,n] line; a longer one goes in parts of PART_DATA bytes. Split points sit on
# multiples of PART_DATA, stepped back at most 3 bytes off a UTF-8 continuation byte, so every part but the last holds
# PART_DATA-3..PART_DATA+3 bytes, the last at least 1, and K = ceil(L / PART_DATA).
PART_DATA = 2880
SEQ_ROW = 18          # what a throwaway sequence adds besides its value: "^?SEQ,SAVE,HILPnn,"
OVER = PULL_MAX + 150  # the reply size the parts tests grow W2 to: 2 parts, clear of the limit whatever PEERSLIVE does
# The secrets in the chain head (collectConfigCommands, WCB.ino): a part boundary inside one would split it across two
# lines, and the harness's redaction (hil/checkpoint.py) keys on the whole token.
SECRET_TOKEN = re.compile(r"\^.(EPASS|WIFI,(?:AP|JOIN))\b[^^]*")


def backup_chain_lines(lines):
    """{"configured": line, "factory": line}: each chain of a ?backup; "" when its section holds no chain, None when the
    section is missing.

    The chain is searched for, not taken from the line after its header. printBackupConfig prints the header, then
    builds the chain in its 2 KB writer through a whole collectConfigCommands pass and only then writes it (WCB.ino),
    so a line another task prints meanwhile can fall between the two: W2's "[ETM] WCB1 came ONLINE (boot)", printed
    from the receive callback on the WiFi task (audit F18), did in wizard.remote_pull (run 20260924-234056). The search
    stops at the section's Checksum line, the next header or the end of the backup, so a missing configured chain
    never picks up the factory one. Failing a checksummed line it gives the first line holding a '^' - a chain split
    by such a line, or printed bare - for backup_chain_problems to report."""
    out = {}
    for name, header in (("configured", "=== For Configured Boards"), ("factory", "=== For Factory Reset")):
        i = next((k for k, x in enumerate(lines) if header in x), None)
        if i is None:
            out[name] = None
            continue
        chain = split = None
        for x in lines[i + 1:]:
            s = x.strip()
            if CHK_LINE.match(s):
                chain = s
                break
            if "=== For " in s or " Checksum: " in s or "End of Backup" in s:
                break
            if split is None and "^" in s:
                split = s
        out[name] = chain or split or ""
    return out


def backup_chain_problems(lines, when="?backup"):
    """Both one-line chains present, not empty and CRC-valid. Reports lengths only: the chains carry ?EPASS and
    ?WIFI, which must never reach a log line or a report."""
    problems = []
    for name, line in backup_chain_lines(lines).items():
        if line is None:
            problems.append(f"{when}: no {name} chain section")
            continue
        m = CHK_LINE.match(line)
        if not m or not m.group(1):
            problems.append(f"{when}: the {name} chain is not a checksummed chain ({len(line)} chars)")
        elif chain_crc(m.group(1)) != m.group(2).upper():
            problems.append(f"{when}: the {name} chain ({len(line)} chars) fails its CRC")
    return problems


@test("wcb.backup_crc", "?backup's configured-boards chain carries a valid CRC-32", needs=["wcb1"])
def backup_crc(bench):
    tokens, provided, calc = usb_wcb(bench).backup_chain()
    assert provided == calc, f"?backup chain says CHK {provided}, CRC-32 of the chain is {calc}"
    assert tokens[0].startswith("?HW,"), f"chain should start with ?HW, starts with {tokens[0]}"


@test("wcb.mgmt_pull", "?MGMT,PULL returns each remote WCB's config: valid CRC, same firmware, right id",
      needs=["wcb1"])
def mgmt_pull(bench):
    w = usb_wcb(bench)
    fw = w.version()
    for n in remote_wcbs(bench):
        ver, tokens, provided, calc = w.mgmt_pull(n)
        assert provided == calc, f"W{n} pull CHK {provided}, calculated {calc}"
        assert ver == fw, f"W{n} runs {ver}, W1 runs {fw}"
        assert f"?WCB,{n}" in tokens, f"W{n} config does not say ?WCB,{n}"


@test("wcb.unknown", "An unknown ? command is reported, not silently swallowed", needs=["wcb1"])
def unknown(bench):
    lines = usb_wcb(bench).run("?ZZHIL")
    assert any(t.startswith("Unknown command: ZZHIL") for t in lines), f"got {lines}"


@test("wcb.help_trap", "A ? command ending in '?' prints help instead of running (and changes nothing)",
      needs=["wcb1"])
def help_trap(bench):
    with config_guard(bench, 1):
        w = usb_wcb(bench)
        lines = w.run("?LABEL,S4,HILTRAP?")
        # A trailing '?' on a command WITH arguments prints the top-level reference (printCommandHelp gets the whole
        # text, which names no section); the bare '?LABEL?' prints the section. Both are help, neither runs.
        assert any("Wireless Communication Board (WCB) - Command Reference" in t for t in lines), f"no help printed: {lines[:6]}"
        assert not any("label set to" in t for t in lines), f"label was set: {lines}"
        assert any("Usage: ?LABEL,Sx,<text>" in t for t in w.run("?LABEL?")), "?LABEL? did not print the LABEL section (WCB_Help.cpp)"


@test("wcb.backup_large_config", "?backup of a config near 6 KB prints both one-line chains whole, with valid CRCs and every sequence in them; with WiFi in AP mode they used to print empty or as a bare checksum from about 2.5 KB (tracker #90)", needs=["wcb1"])
def backup_large_config(bench):
    """printBackupConfig prints each chain a token at a time with a running CRC (WCB.ino). The throwaway sequences are
    plain text that nothing recalls; they are removed again, and config_guard checks W1 is as it was."""
    w = usb_wcb(bench)
    keys, problems = [], []
    with config_guard(bench, 1):
        try:
            for i in range(10):
                key = f"HILB{i:02d}"
                out = w.run(f"?SEQ,SAVE,{key},HILB{'z' * 496}", timeout=8)
                if not any(f"Stored: Key='{key}'" in x for x in out):
                    break                                        # NVS is full: what is stored has to do
                keys.append(key)
            lines = w.run("?backup", timeout=15)
            chains = backup_chain_lines(lines)
            sizes = {k: len(v or "") for k, v in chains.items()}
            bench.note(f"{len(keys)} sequences of 500 characters stored; chains {sizes['configured']} and "
                       f"{sizes['factory']} characters")
            assert sizes["factory"] >= 4500, (f"the factory chain is {sizes['factory']} characters after {len(keys)} "
                                               f"sequences; the test needs 4500+, where both chains used to fail")
            problems += backup_chain_problems(lines)
            for name, line in chains.items():
                missing = [k for k in keys if not line or f"?SEQ,SAVE,{k},HILB" not in line]
                if missing:
                    problems.append(f"the {name} chain lacks {missing}")
            heap = [x for x in w.run("?STATS", timeout=6) if x.startswith("Heap:")]
            if heap:
                bench.note(f"W1 after the backup: {heap[0]}")
        finally:
            for k in keys:
                w.run(f"?SEQ,CLEAR,{k}")
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ the mesh config pull at size (F13)
# W1 relays, W2 is the target, both on their own USB: W2's own ?backup is the reference for what a pull of it must join
# to, and its RAM-only test knobs (?DEBUG,PULLPART / ?DEBUG,PULLFAULT) go straight to it. A pull body carries ?EPASS
# and ?WIFI, so every note and assertion here gives lengths, counts, offsets, token names and codes - never text.
def _w2(bench):
    return WCB(bench.dev("wcb2"))


def _factory_reply(w2, ver, tries=3):
    """What a pull of W2 must come to: '[VER:<fw>]' + the factory chain of W2's own ?backup, the chain buildConfigString
    puts behind its version. Carries the password: compare it, never print it. A chain that fails its CRC is read
    again (?backup only reads): one longer than the 2 KB write buffer leaves in pieces, and a line printed on the WiFi
    task can still land between two of them (audit F18). backup_large_config is what tests the chain itself."""
    for _ in range(tries):
        line = backup_chain_lines(w2.run("?backup", timeout=10))["factory"] or ""
        m = CHK_LINE.match(line)
        if m and m.group(1) and chain_crc(m.group(1)) == m.group(2).upper():
            return f"[VER:{ver}]{line}"
    raise AssertionError(f"W2's ?backup had no CRC-valid factory chain in {tries} reads (the last: {len(line)} chars)")


def _grow(w2, ver, keys, target):
    """Store throwaway HILP<nn> sequences on W2 - plain 'z' text nothing recalls - until its pull reply is at least
    `target` characters -> the length reached. Stops early when NVS refuses one: what is stored has to do, and the
    caller asserts the size it needs (W2 had 137 NVS entries free on 2026-09-24; an 800-character value takes 27)."""
    n = len(_factory_reply(w2, ver))
    while n < target and len(keys) < 12:
        key, vlen = f"HILP{len(keys):02d}", max(1, min(800, target - n - SEQ_ROW))
        out = w2.run(f"?SEQ,SAVE,{key},{'z' * vlen}", timeout=8)
        if not any(f"Stored: Key='{key}'" in x for x in out):
            break                                           # NVS is full
        keys.append(key)
        n = len(_factory_reply(w2, ver))
    return n


def _grow_over(w2, ver, keys):
    """_grow W2 to OVER: a reply over PULL_MAX, which is what every parts test needs -> its length."""
    n = _grow(w2, ver, keys, OVER)
    assert n > PULL_MAX, f"W2's reply reached {n} characters before NVS refused more; the test needs over {PULL_MAX}"
    return n


def _clear(w2, keys):
    for k in keys:
        w2.run(f"?SEQ,CLEAR,{k}")


def _knob(w2, cmd, check=True):
    """A RAM-only F13 test knob, on W2's own USB. A firmware from before F13 answers 'Invalid DEBUG command'."""
    out = w2.run(cmd)
    if check and any(re.search(r"Invalid|Unknown|Usage", x) for x in out):
        raise AssertionError(f"W2 does not take {cmd} - is it running the F13 firmware? It said {out[:2]}")
    return out


def _names(tokens):
    """Token names without their values ('?SEQ,SAVE,HILP01', '?EPASS'), at most 6."""
    return [",".join(t.split(",")[:3]) if t.upper().startswith("?SEQ,SAVE,") else t.split(",")[0] for t in tokens[:6]]


def _chain_of(text):
    """'[VER:x]<chain>^?CHK<8 hex>' -> (the [VER:] head, the chain's comparable tokens: PEERSLIVE out, ^-values
    joined)."""
    head = text[:text.find("]") + 1] if text.startswith("[VER:") else ""
    m = CHK_LINE.match(text[len(head):])
    return head, comparable((m.group(1) if m else text[len(head):]).split("^"))


def _same_reply(got, want):
    """'' when a pulled reply is W2's own factory chain behind [VER:], or why not - in lengths and token names only.
    PEERSLIVE counts live peers and changes with no command, so a reply that differs only there (and so in its CHK) is
    the same config."""
    if got == want:
        return ""
    (gh, a), (wh, b) = _chain_of(got), _chain_of(want)
    if gh == wh and a == b:
        return ""
    missing, extra = [t for t in b if t not in a], [t for t in a if t not in b]
    return (f"{len(got)} characters pulled, W2's own chain is {len(want)}"
            + ("" if gh == wh else f", under another [VER:] ({len(gh)} vs {len(wh)} characters)")
            + f"; {len(missing)} tokens missing {_names(missing)}, {len(extra)} extra {_names(extra)}")


def _reply_problems(r, want, keys, kind, count=None):
    """A pulled reply against W2's own chain: its kind (and part count), CRC, equality, every throwaway key there."""
    problems = []
    if r.kind != kind or (count is not None and r.count != count):
        problems.append(f"arrived as {r.count} {r.kind} line(s), expected {count or 'several'} {kind}")
    if r.provided != r.calc:
        problems.append(f"the {len(r.text)}-character reply fails its CRC")
    why = _same_reply(r.text, want)
    if why:
        problems.append(why)
    missing = [k for k in keys if not any(t.startswith(f"?SEQ,SAVE,{k},") for t in group_tokens(r.tokens))]
    if missing:
        problems.append(f"the pull lacks {missing}")
    return problems


def _part_problems(r, d):
    """The part geometry F13 promises, over the reply's bytes: K = ceil(L/d), every part but the last d-3..d+3 bytes,
    the last at least 1."""
    sizes, total = [len(p.encode()) for p in r.parts], len(r.text.encode())
    problems = []
    if len(sizes) != math.ceil(total / d):
        problems.append(f"{len(sizes)} parts for a {total}-byte reply at {d} bytes a part, expected "
                        f"{math.ceil(total / d)}")
    if any(not d - 3 <= s <= d + 3 for s in sizes[:-1]) or (sizes and sizes[-1] < 1):
        problems.append(f"part sizes {sizes}: all but the last must be {d - 3}..{d + 3} bytes, the last at least 1")
    return problems


def _secret_problems(r):
    """?EPASS (always there) and any ?WIFI,AP|JOIN token must end inside part 1: a secret split across two lines leaves
    its tail outside what the redaction recognises. Offsets only."""
    found = [(m.group(1), m.end()) for m in SECRET_TOKEN.finditer(r.text)]
    problems = [] if any(name == "EPASS" for name, _ in found) else ["the pull has no ?EPASS token"]
    first = r.part_lengths[0] if r.part_lengths else len(r.text)
    problems += [f"the ?{name} token ends at character {end}, past part 1 ({first} characters)"
                 for name, end in found if end > first]
    return problems


def _pull_lines(dev, since, wcb):
    """Every pull line for W<wcb> on `dev` after `since`, without its text: {'order': [tags in arrival order],
    'CONFIG': [body lengths, 0 = empty], 'CFGPART': [(id, k, K, data length), or None if malformed],
    'CFGERR': [(code, detail)]}. The code is screened (screen_code), because problem messages quote it; the detail is
    not, because _error_text_problems looks in it for config text, and it is never quoted."""
    out = {"order": [], "CONFIG": [], "CFGPART": [], "CFGERR": []}
    for line in dev.since(since):
        got = parse_pull_line(line)
        if not got or got[1] != wcb:
            continue
        tag, _, body = got
        out["order"].append(tag)
        if tag == "CONFIG":
            out["CONFIG"].append(len(body.strip()))
        elif tag == "CFGPART":
            p = parse_part(body)
            out["CFGPART"].append(p and (p[0], p[1], p[2], len(p[3])))
        else:
            code, detail = parse_error(body)
            out["CFGERR"].append((screen_code(code), "".join(c if " " <= c <= "~" else "?" for c in detail)[:120]))
    return out


def _error_text_problems(seen):
    return [f"the CFGERR {code} line carries config text" for code, detail in seen["CFGERR"]
            if CONFIG_TEXT.search(detail)]


def _refused(w1, code, problems, parts=True, rearm=None):
    """Pull W2 through W1, expecting CFGERR <code> -> (the mark the pull was sent at, the PullRefused or None). A whole
    reply, or another code, is a problem; no answer at all raises. The first answer is taken as it came (no re-send on
    NOPARTS: see hil/wcb.py Pull), except with `rearm`, which re-arms W2's one-shot ?DEBUG,PULLFAULT: a parts pull
    answered NOPARTS lost every copy of its parts request, and W2 spent the fault on that job (cpjStart disarms it on
    accepting any request), so the fault is armed again and the pull goes once more. The second answer counts, and the
    mark returned is its own."""
    for attempt in (1, 2):
        p = Pull(w1.dev, 2, parts=parts, timeout=10, retry_noparts=False)
        try:
            while not p.poll():
                time.sleep(0.05)
        except PullRefused as e:
            if attempt == 1 and rearm and parts and e.code == "NOPARTS" and code != "NOPARTS":
                problems.extend(_error_text_problems(_pull_lines(w1.dev, p.mark, 2)))
                dev_note(w1.dev, f"{p.cmd} was answered CFGERR NOPARTS (a lost parts request), which spent W2's "
                                 f"one-shot fault: arming it again and pulling once more")
                rearm()
                continue
            if e.code != code:
                problems.append(f"the refusal is {e.code} ({e.detail}), not {code}")
            return p.mark, e
        r = p.reply(verify=False)
        problems.append(f"the pull returned {r.count} {r.kind} line(s), {len(r.text)} characters, instead of CFGERR "
                        f"{code}")
        return p.mark, None


@test("wcb.pull_size_limit", "?MGMT,PULL,2,P of a config just under the relay's 2912-character limit arrives as the one [MGMT:CONFIG,2] line, exactly W2's own factory chain; one over it arrives as 2 [MGMT:CFGPART,2] parts that join into that chain, CRC-valid, with ?EPASS and ?WIFI whole inside part 1, and never as a config line (tracker #90, F13)", needs=["wcb1", "wcb2"])
def pull_size_limit(bench):
    """A reply of PULL_MAX characters or less goes exactly as before F13; a longer one goes in parts to a request that
    asked for them (handleConfigReqPacket). The pull body is W2's factory chain behind [VER:<version>], so W2's own
    ?backup is the reference for both. The throwaway sequences on W2 are removed again."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    keys, problems = [], []
    with config_guard(bench, 2):
        try:
            ver = w2.version()
            n = len(_factory_reply(w2, ver))
            assert n < PULL_MAX - 100, f"W2's config is already {n} characters; the test needs room under {PULL_MAX}"
            # Fill to 10 under the limit.
            for _ in range(10):
                room = PULL_MAX - 10 - n - SEQ_ROW
                if room < 1:
                    break
                key, vlen = f"HILP{len(keys):02d}", min(800, room)
                out = w2.run(f"?SEQ,SAVE,{key},{'z' * vlen}", timeout=8)
                assert any(f"Stored: Key='{key}'" in x for x in out), f"W2 did not store {key} ({vlen} characters)"
                keys.append(key)
                n = len(_factory_reply(w2, ver))
            assert PULL_MAX - 40 <= n <= PULL_MAX, f"W2's config came to {n} characters, not just under {PULL_MAX}"

            want = _factory_reply(w2, ver)
            r = w1.pull_reply(2, timeout=15, verify=False)
            bench.note(f"under the limit: W2's reply is {n} characters by its own ?backup; pulled as {r.count} "
                       f"{r.kind} line of {len(r.text)}")
            problems += [f"under the limit: {p}" for p in _reply_problems(r, want, keys, "legacy", 1)]

            key = f"HILP{len(keys):02d}"
            out = w2.run(f"?SEQ,SAVE,{key},{'z' * 60}", timeout=8)
            assert any(f"Stored: Key='{key}'" in x for x in out), f"W2 did not store {key}"
            keys.append(key)
            want = _factory_reply(w2, ver)
            over = len(want)
            assert over > PULL_MAX, f"W2's config came to {over} characters, not over {PULL_MAX}"
            r = w1.pull_reply(2, timeout=15, verify=False)
            time.sleep(1.0)                    # a stray config line after the last part still lands in the window
            bench.note(f"over the limit: W2's reply is {over} characters; pulled as {r.count} {r.kind} of "
                       f"{r.part_lengths} characters")
            problems += [f"over the limit: {p}" for p in _reply_problems(r, want, keys, "parts", 2)
                         + _part_problems(r, PART_DATA) + _secret_problems(r)]
            got = [x for x in _pull_lines(w1.dev, r.mark, 2)["CONFIG"] if x]
            if got:
                problems.append(f"W1 printed {len(got)} non-empty [MGMT:CONFIG,2] line(s) for the {over}-character "
                                f"config, which an old Wizard would store as W2's whole config")
        finally:
            _clear(w2, keys)
    assert not problems, "; ".join(problems)


@test("wcb.pull_plain_over_limit", "A plain ?MGMT,PULL,2 (an old Wizard's: no ,P) of a config over 2912 characters gets one [MGMT:CFGERR,2]NOPARTS line giving its length, and no part and no config line (F13)", needs=["wcb1", "wcb2"])
def pull_plain_over_limit(bench):
    """Parts go only to a request that asked for them (CONFIG_REQ_PARTS), because an old Wizard stores anything under
    [MGMT:CONFIG,n] as the whole config. The refusal rides packet type 18, which an old relay drops unseen."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    keys, problems = [], []
    with config_guard(bench, 2):
        try:
            n = _grow_over(w2, w2.version(), keys)
            mark, e = _refused(w1, "NOPARTS", problems, parts=False)
            if e is not None:
                bench.note(f"W2 at {n} characters, plain pull: CFGERR {e.code} ({e.detail})")
                if e.code == "NOPARTS" and str(n) not in e.detail:
                    problems.append(f"the NOPARTS detail does not give the reply's length {n}: {e.detail!r}")
            time.sleep(3.0)
            seen = _pull_lines(w1.dev, mark, 2)
            if seen["order"] != ["CFGERR"]:
                problems.append(f"W1 printed {seen['order']} for W2; a plain pull over the limit brings one CFGERR "
                                f"line, no part and no config line")
            problems += _error_text_problems(seen)
        finally:
            _clear(w2, keys)
    assert not problems, "; ".join(problems)


@test("wcb.pull_parts_many", "With W2's part size cut to 600 bytes (?DEBUG,PULLPART,600, RAM only), a pull over 2912 characters arrives in K = ceil(L/600) >= 3 parts of one id, middle parts included, that join into W2's factory chain, CRC-valid, ?EPASS whole in part 1 (F13)", needs=["wcb1", "wcb2"])
def pull_parts_many(bench):
    """No bench board holds the 5761+ characters a third 2880-byte part needs (W2 had NVS room for about 3.3 KB on
    2026-09-24), so the knob shrinks the parts instead; the line between one reply and parts stays at 2912. Its floor
    of 512 keeps the secrets of the chain head inside part 1."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    d, keys, problems = 600, [], []
    with config_guard(bench, 2):
        try:
            ver = w2.version()
            n = _grow_over(w2, ver, keys)
            _knob(w2, f"?DEBUG,PULLPART,{d}")
            want = _factory_reply(w2, ver)
            r = w1.pull_reply(2, timeout=15, verify=False)
            time.sleep(1.0)
            bench.note(f"W2 at {len(want)} characters with {d}-byte parts: {r.count} {r.kind} of {r.part_lengths}")
            problems += _reply_problems(r, want, keys, "parts") + _part_problems(r, d) + _secret_problems(r)
            if r.count < 3:
                problems.append(f"{r.count} parts; the test needs 3 or more, so that a middle part is exercised")
            if any(_pull_lines(w1.dev, r.mark, 2)["CONFIG"]):
                problems.append("W1 printed a non-empty [MGMT:CONFIG,2] line during a parts pull")
        finally:
            _knob(w2, "?DEBUG,PULLPART,OFF", check=False)
            _clear(w2, keys)
    assert not problems, "; ".join(problems)


@test("wcb.pull_error_oom_legacy", "Out of memory for a config under 2912 characters (?DEBUG,PULLFAULT,OOM on W2: one-shot, RAM only): W1 prints [MGMT:CFGERR,2]NOMEM, then the empty [MGMT:CONFIG,2] an old Wizard shows as 'empty config response', no config text in either; the next pull is whole (F13)", needs=["wcb1", "wcb2"])
def pull_error_oom_legacy(bench):
    """The fault fails the reply buffer's malloc on every retry, so the real NOMEM branch runs: 1 s of 200 ms retries,
    then the error. Out of memory cannot be had on demand otherwise (W2's largest free block is 14-18 KB). The empty
    line after the error is for old Wizards: they ignore CFGERR, and the empty body is the one reply they refuse."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    with config_guard(bench, 2):
        try:
            n = len(_factory_reply(w2, w2.version()))
            assert n <= PULL_MAX, f"W2's reply is {n} characters; this test needs a one-line config (<= {PULL_MAX})"
            _knob(w2, "?DEBUG,PULLFAULT,OOM")
            mark, e = _refused(w1, "NOMEM", problems)
            time.sleep(3.0)
            seen = _pull_lines(w1.dev, mark, 2)
            bench.note(f"W2 at {n} characters, out of memory: W1 printed {seen['order']}"
                       + (f"; CFGERR {e.code} ({e.detail})" if e else ""))
            if seen["order"] != ["CFGERR", "CONFIG"] or seen["CONFIG"] != [0]:
                problems.append(f"W1 printed {seen['order']} (config bodies of {seen['CONFIG']} characters) for W2; "
                                f"expected the CFGERR, then one empty [MGMT:CONFIG,2]")
            problems += _error_text_problems(seen)
            r = w1.pull_reply(2, timeout=10)              # the fault is one-shot: this one must be whole
            if r.kind != "legacy":
                problems.append(f"the pull after the fault came as {r.count} {r.kind} line(s), expected one")
        finally:
            _knob(w2, "?DEBUG,PULLFAULT,OFF", check=False)
    assert not problems, "; ".join(problems)


@test("wcb.pull_error_oom_parts", "Out of memory for a config over 2912 characters (?DEBUG,PULLFAULT,OOM on W2: one-shot, RAM only): W1 prints only [MGMT:CFGERR,2]NOMEM - no part, no config line, no config text; the next pull arrives whole in parts (F13)", needs=["wcb1", "wcb2"])
def pull_error_oom_parts(bench):
    """Unlike the one-line case there is no empty [MGMT:CONFIG,2] after the error: an old Wizard never gets parts, so
    there is nothing to tell it."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    keys, problems = [], []
    with config_guard(bench, 2):
        try:
            ver = w2.version()
            n = _grow_over(w2, ver, keys)
            _knob(w2, "?DEBUG,PULLFAULT,OOM")
            mark, e = _refused(w1, "NOMEM", problems, rearm=lambda: _knob(w2, "?DEBUG,PULLFAULT,OOM"))
            time.sleep(3.0)
            seen = _pull_lines(w1.dev, mark, 2)
            bench.note(f"W2 at {n} characters, out of memory: W1 printed {seen['order']}"
                       + (f"; CFGERR {e.code} ({e.detail})" if e else ""))
            if seen["order"] != ["CFGERR"]:
                problems.append(f"W1 printed {seen['order']} for W2; expected one CFGERR line and nothing else")
            problems += _error_text_problems(seen)
            want = _factory_reply(w2, ver)
            r = w1.pull_reply(2, timeout=15, verify=False)
            problems += [f"the pull after the fault: {p}" for p in _reply_problems(r, want, keys, "parts", 2)]
        finally:
            _knob(w2, "?DEBUG,PULLFAULT,OFF", check=False)
            _clear(w2, keys)
    assert not problems, "; ".join(problems)


# The longest one config walk may hold loop(): well under the 640 ms that sending even one 16-frag message in a single
# call takes (16 frags x 2 passes x delay(20)), and generous for a walk of a 3 KB config.
WALK_MAX_S = 0.4


def _version_rtt(w2):
    """Seconds from sending ?VERSION on W2's USB to its 'Software Version:' line: how long W2's loop() takes to come
    round to its USB command queue, which it drains one command per pass."""
    m = w2.dev.mark()
    t0 = time.monotonic()
    w2.dev.send("?VERSION")
    w2.dev.expect(r"^Software Version:", timeout=3, since=m)
    return time.monotonic() - t0


@test("wcb.pull_nonblocking", "While W2 sends a 2-part config to W1 it keeps answering ?VERSION on its own USB within 150 ms (one slower reply allowed, for a config walk): the reply goes out a frag per loop() pass, not in one blocking call (F13)", needs=["wcb1", "wcb2"])
def pull_nonblocking(bench):
    """Before F13 the target sent a whole reply inside one call, delay(20) after every frag: about 640 ms of a stalled
    loop() for 16 frags, with the USB command queue, the ETM ACK ring and ETM retries waiting behind it. Now a job sends
    one frag per serviceConfigPullJob() call; the walks that measure a config and build a message are the only blocking
    steps left. Sampling runs on until 1 s after W1 has the reply, because W2 is still sending its second pass then.
    The one slower reply allowed must still be under WALK_MAX_S: a send that blocks for the whole reply delays exactly
    one sample too - the one waiting behind it - so '150 ms but one' alone passed a simulated blocking target."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    keys = []
    with config_guard(bench, 2):
        try:
            n = _grow_over(w2, w2.version(), keys)
            idle = [_version_rtt(w2) for _ in range(5)]
            samples, done_at = [], None
            p = Pull(w1.dev, 2, parts=True, timeout=10)
            t0 = time.monotonic()
            while done_at is None or time.monotonic() - done_at < 1.0:
                if done_at is None and p.poll():
                    done_at = time.monotonic()
                t = time.monotonic()
                samples.append(_version_rtt(w2))
                time.sleep(max(0.0, 0.05 - (time.monotonic() - t)))
            r = p.reply()
            slow = sorted(round(s * 1000) for s in samples if s > 0.150)
            bench.note(f"W2 at {n} characters: {r.count}-part pull done in {done_at - t0:.2f} s; {len(samples)} "
                       f"?VERSION round trips, max {max(samples) * 1000:.0f} ms, over 150 ms: {slow}; idle max "
                       f"{max(idle) * 1000:.0f} ms")
        finally:
            _clear(w2, keys)
    assert r.kind == "parts" and r.count == 2, (f"the pull came as {r.count} {r.kind} line(s); the test needs a "
                                                f"2-part job")
    assert len(samples) >= 10, f"only {len(samples)} ?VERSION round trips fit in the pull, too few to measure it"
    assert len(slow) <= 1, f"{len(slow)} ?VERSION round trips took over 150 ms while W2 sent its config: {slow} ms"
    assert not slow or slow[0] < WALK_MAX_S * 1000, (f"a ?VERSION round trip took {slow[0]} ms while W2 sent its "
                                                     f"config: loop() blocked for the send, not just a config walk")


# ------------------------------------------------------------------ the target's scheduler and the restart gate (WCB-WP25)
# W2's ?DEBUG,MGMT lines (RAM only; a reboot clears the flag) are the scheduler's own record, all printed on the loop task:
# cpjStart 'Config request from WCB<r>[ (parts accepted)]' for a request it takes, handleConfigReqPacket 'Duplicate config
# request from WCB<r> ignored', '... ignored: reply in progress' and '... parked behind WCB<x>'s reply', cpjMessageDone
# 'Config pull to WCB<r> sent (<L> chars)' (all WCB.ino). Line times are the host's (hil/serialdev.py): a line can be
# stamped late, never early (HIL_TESTING.md §6).
ACCEPTED_W1 = r"^\[MGMT\] Config request from WCB1(?: \(parts accepted\))?$"
SENT_W1 = r"^\[MGMT\] Config pull to WCB1 sent \(\d+ chars\)$"
QUIET_S = 4.0          # PWM_REBOOT_QUIET_MS (WCB.ino): a deferred restart waits this long with no command processed
DEDUP_S = 1.5          # CPJ_DEDUP_MS (WCB.ino): a requester's requests are ignored this long after one is accepted


def _at(dev, since, pattern):
    """(host time, line index) of the first line after `since` matching `pattern`, or (None, None)."""
    rx = re.compile(pattern)
    for i, (t, x) in enumerate(list(dev.lines[since:])):
        if rx.search(x):
            return t, since + i
    return None, None


def _stamp(dev, wcb):
    """Tell hil.wcb.Pull that a pull of W<wcb> just left `dev` by hand. Pull spaces a pull PULL_SPACING_S from its own
    last stamp only (Pull._send), so after a raw ?MGMT,PULL the next Pull could go inside the target's 1.5 s dedup
    window, be dropped, and time out."""
    dev.__dict__.setdefault("_last_pull", {})[wcb] = time.monotonic()


def _w2_ready(w2, wait=30.0):
    """W2's version once it answers again: a finally can run while W2 is still booting from a restart this test asked
    for, and a line sent in its first second after reset is flushed (HIL_TESTING.md §5)."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return w2.version()
        except AssertionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.0)


def _w2_online(bench, w1, wait=25.0):
    """After W2 restarted: ?WDP,POLL on W1, then wait for W1's ?STATS to show WCB2 online again - a peer that has just
    booted is offline to W1 until its next packet (HIL_TESTING.md §6), and the next test's unicast would go unretried.
    Notes instead of raising: it runs in a finally."""
    t0 = time.monotonic()
    w1.run("?WDP,POLL")
    time.sleep(1.0)            # the adverts it asks for print '[ETM] WCBn came ONLINE' for up to ~0.7 s (F18)
    while time.monotonic() - t0 < wait:
        if w1.etm_board_stats().get(2, {}).get("online"):
            return
        time.sleep(1.0)
    bench.note(f"W1 still saw WCB2 offline {wait:.0f} s after W2's restart")


def _restart_arm(w1, w2, delay, want, keys):
    """One ?reboot of W2 raced against a 6-part pull of it -> (problems, straddled, (accepted, sent, restart) seconds
    after 'Reboot queued' or None). delay None: ?reboot once W2 has taken the pull; a number: the pull goes that many
    seconds after W2's 'Reboot queued'. straddled: W2's own log shows the job taken before the quiet window ran out and
    sent after it, with the restart right behind the 'sent' line - the case only the configPullJobActive() term covers.
    Knobs first each time: a boot clears them."""
    problems = []
    _knob(w2, "?DEBUG,PULLPART,512")
    _knob(w2, "?DEBUG,MGMT,ON")
    m2 = w2.dev.mark()
    if delay is None:
        p = Pull(w1.dev, 2, parts=True, timeout=10)
        w2.dev.expect(ACCEPTED_W1, timeout=4, since=m2)
        w2.dev.send("?reboot")               # raw: WCB.run's echo would be one more command in the quiet window
    else:
        w2.dev.send("?reboot")
        w2.dev.expect(r"^Reboot queued", timeout=3, since=m2)
        tq, _ = _at(w2.dev, m2, r"^Reboot queued")
        time.sleep(max(0.0, tq + delay - time.monotonic()))
        p = Pull(w1.dev, 2, parts=True, timeout=10)
    try:
        while not p.poll():
            time.sleep(0.05)
        problems += _reply_problems(p.reply(verify=False), want, keys, "parts")
    except AssertionError as e:               # PullRefused is one too
        problems.append(f"the pull did not complete: {e}")
    w2.wait_boot(m2, timeout=40)
    tq, _ = _at(w2.dev, m2, r"^Reboot queued")
    ta, _ = _at(w2.dev, m2, ACCEPTED_W1)
    ts, i_sent = _at(w2.dev, m2, SENT_W1)
    tb, i_boot = _at(w2.dev, m2, r"^Rebooting now")
    if i_boot is None:
        problems.append("W2 printed no 'Rebooting now...' before its boot banner")
    elif i_sent is None or i_boot < i_sent:
        problems.append("W2 restarted before 'Config pull to WCB1 sent': the restart did not wait for the job")
    if None in (tq, ta, ts, tb):
        return problems, False, None
    t = (round(ta - tq, 2), round(ts - tq, 2), round(tb - tq, 2))
    straddled = ta < tq + QUIET_S - 0.1 and ts > tq + QUIET_S + 0.1 and tb - ts < 0.5
    return problems, straddled, t


@test("wcb.pull_holds_restart", "A deferred ?reboot on W2 waits for a config pull W2 is sending: a 6-part pull (?DEBUG,PULLPART,512) running when ?reboot arrives, and one landing 3.3/3.6/3.9 s after it so the 4 s quiet window runs out mid-job, both arrive whole and CRC-valid at W1, and W2 restarts only after its 'Config pull to WCB1 sent' (WCB-WP25; 2-4 W2 reboots)", needs=["wcb1", "wcb2"])
def pull_holds_restart(bench):
    """The restart gate in loop() (WCB.ino) is '(quiet || capped) && !configPullJobActive()', and a config request
    never moves the quiet clock: it reaches the target through drainMgmtReqs, not through the command queue that stamps
    lastCommandProcessedMs. Arm 1 sends ?reboot while the parts flow; the job (~1 s) ends inside the 4 s window, so it
    shows only that nothing restarts early. Arm 2 would catch a lost configPullJobActive() term: the pull lands just
    before the window runs out, so the restart falls due mid-job and must wait for the job's end. An offset counts only
    when W2's log shows it straddled that moment (see _restart_arm); a mesh command in the window moves the moment, and
    the next offset is tried. The throwaway HILP sequences are removed again."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    keys, problems, seen = [], [], []
    with config_guard(bench, 2):
        try:
            ver = w2.version()
            n = _grow_over(w2, ver, keys)
            want = _factory_reply(w2, ver)
            got, straddled, t = _restart_arm(w1, w2, None, want, keys)
            problems += [f"?reboot during the pull: {x}" for x in got]
            seen.append(("during", t))
            for delay in (3.3, 3.6, 3.9):
                got, straddled, t = _restart_arm(w1, w2, delay, want, keys)
                problems += [f"pull {delay} s after ?reboot: {x}" for x in got]
                seen.append((delay, t))
                if straddled or got:
                    break
            bench.note(f"W2 at {n} characters in 512-byte parts; (arm, (accepted, sent, restart) s after 'Reboot "
                       f"queued'): {seen}")
            if not problems and not straddled:
                problems.append(f"no pull had the {QUIET_S:.0f} s quiet window run out mid-job, so the "
                                f"configPullJobActive() term went untested: {seen}")
        finally:
            try:
                _w2_ready(w2)
                _knob(w2, "?DEBUG,PULLPART,OFF", check=False)
                w2.run("?DEBUG,MGMT,OFF")
            finally:
                _clear(w2, keys)
                _w2_online(bench, w1)
    assert not problems, "; ".join(problems)


@test("wcb.pull_changed_restart", "A config that changes while W2 sends it in parts is sent again under a new part id, whole, with the change in it; one that keeps changing (a label every 150 ms) is answered [MGMT:CFGERR,2]CHANGED after 3 walks, which W2 also prints, and the next pull is whole (WCB-WP25)", needs=["wcb1", "wcb2"])
def pull_changed_restart(bench):
    """Every build walk re-checks the reply's length and CRC against the measure walk (cpjBuild, WCB.ino). A mismatch
    starts the job over with a new id (cpjMeasure draws one that differs), at most CPJ_MAX_RESTARTS (2) times; then
    cpjFail(CHANGED) sends 'ECHANGED,the config changed while it was sent (3 tries); retry' (cfgpErrorText,
    WCB_ConfigParts.h) and prints the ungated '[MGMT] Config pull from WCB1: the config changed on every walk (3
    tries).' line. The measure walk and part 1's build run in one step, so a change lands after part 1 at the
    earliest - which is when this test makes it. The requester drops the parts of an id a new one replaces
    (PullCollector.restarts counts that). The change is ?LABEL,S5 on W2's own USB, put back at the end."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    keys, problems = [], []
    with config_guard(bench, 2) as before:
        restore = token(before[2], "?LABEL,S5,") or "?LABEL,CLEAR,S5"
        try:
            ver = w2.version()
            n = _grow_over(w2, ver, keys)
            _knob(w2, "?DEBUG,PULLPART,512")
            _knob(w2, "?DEBUG,MGMT,ON")
            label = marker("L")
            # One change, just after part 1 arrives.
            m2 = w2.dev.mark()
            p = Pull(w1.dev, 2, parts=True, timeout=10)
            changed = False
            while not p.poll():
                if not changed and p.collector.parts:
                    w2.dev.send(f"?LABEL,S5,{label}")
                    changed = True
                time.sleep(0.02)
            r = p.reply(verify=False)
            ids = sorted({x[0] for x in _pull_lines(w1.dev, r.mark, 2)["CFGPART"] if x})
            want = _factory_reply(w2, ver)
            bench.note(f"W2 at {n} characters in 512-byte parts, S5 relabelled after part 1: {r.count} parts under "
                       f"{len(ids)} id(s), {p.collector.restarts} collector restart(s)")
            problems += [f"one change: {x}" for x in _reply_problems(r, want, keys, "parts")]
            if not changed:
                problems.append("one change: the reply was whole before part 1 was seen, so nothing was changed")
            elif p.collector.restarts < 1 or len(ids) < 2:
                problems.append(f"one change: the reply came under {len(ids)} part id(s) with {p.collector.restarts} "
                                f"restart(s); a change after part 1 must restart it under a new id")
            if f"?LABEL,S5,{label}" not in r.tokens:
                problems.append("one change: the reply lacks the new S5 label")
            if not any(re.search(r"^\[MGMT\] Config pull to WCB1: the config changed between walks - starting over",
                                 x) for x in w2.dev.since(m2)):
                problems.append("one change: W2 did not log 'the config changed between walks - starting over'")
            # A change every 150 ms: every walk sees a new config.
            m2 = w2.dev.mark()
            p = Pull(w1.dev, 2, parts=True, timeout=10, retry_noparts=False)
            refused, i, nxt = None, 0, time.monotonic()
            try:
                while True:
                    if time.monotonic() >= nxt:
                        w2.dev.send(f"?LABEL,S5,{label}{i}")
                        i, nxt = i + 1, time.monotonic() + 0.15
                    if p.poll():
                        break
                    time.sleep(0.01)
            except PullRefused as e:
                refused = e
            except AssertionError as e:
                problems.append(f"churn: {e}")
            time.sleep(0.5)
            if refused is None and p.collector.reply is not None:
                r = p.reply(verify=False)
                problems.append(f"churn: the pull completed as {r.count} {r.kind} line(s) while S5 was relabelled "
                                f"every 150 ms ({i} times)" + ("" if r.provided == r.calc else ", and fails its CRC"))
            elif refused is not None and refused.code != "CHANGED":
                problems.append(f"churn: refused {refused.code} ({refused.detail}), not CHANGED")
            elif refused is not None and "(3 tries)" not in refused.detail:
                problems.append(f"churn: the CHANGED detail does not say 3 tries: {refused.detail!r}")
            if not any(x.startswith("[MGMT] Config pull from WCB1: the config changed on every walk (3 tries).")
                       for x in w2.dev.since(m2)):
                problems.append("churn: W2 did not print 'the config changed on every walk (3 tries)'")
            problems += [f"churn: {x}" for x in _error_text_problems(_pull_lines(w1.dev, p.mark, 2))]
            bench.note(f"churn: {i} relabels; W1's pull ended in "
                       + (f"CFGERR {refused.code}" if refused else "no refusal"))
            w2.run(restore)
            want = _factory_reply(w2, ver)
            r = w1.pull_reply(2, timeout=15, verify=False)
            problems += [f"after the churn: {x}" for x in _reply_problems(r, want, keys, "parts")]
        finally:
            try:
                w2.run(restore)
                _knob(w2, "?DEBUG,PULLPART,OFF", check=False)
                w2.run("?DEBUG,MGMT,OFF")
            finally:
                _clear(w2, keys)
    assert not problems, "; ".join(problems)


@test("wcb.pull_dedup_window", "W2 answers a requester once per 1.5 s: W1's ?MGMT,PULL,2 sent 1.0 s after W2 took the previous one is dropped as a duplicate and gets no reply, one sent 1.7 s after it (the harness's PULL_SPACING_S) is answered whole (WCB-WP25)", needs=["wcb1", "wcb2"])
def pull_dedup_window(bench):
    """handleConfigReqPacket (WCB.ino) stamps a requester when it accepts a request (cpjStart) and ignores that
    requester for CPJ_DEDUP_MS (1500) afterwards, whether the request is one of the relay's own copies or a genuine
    re-pull, and an ignored one does not move the stamp. Both re-sends are timed from W2's own acceptance line, which
    the host can only stamp late: the 1.0 s one reaches W2 well inside the window, the 1.7 s one never before its end.
    A plain pull of W2's one-line config. Read-only apart from ?DEBUG,MGMT (RAM)."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    try:
        n = len(_factory_reply(w2, w2.version()))
        if n > PULL_MAX:
            raise Skip(f"W2's config is {n} characters, over the one-line limit - a leftover from an aborted size test?")
        _knob(w2, "?DEBUG,MGMT,ON")
        m1, m2 = w1.dev.mark(), w2.dev.mark()
        w1.pull_reply(2, parts=False, timeout=10)
        ta, _ = _at(w2.dev, m2, ACCEPTED_W1)
        if ta is None:
            raise AssertionError("W2 logged no 'Config request from WCB1' for the first pull (?DEBUG,MGMT)")
        time.sleep(max(0.0, ta + 1.0 - time.monotonic()))
        md = w2.dev.mark()
        w1.dev.send("?MGMT,PULL,2")
        _stamp(w1.dev, 2)
        time.sleep(max(0.0, ta + 1.7 - time.monotonic()))
        mr, m1r = w2.dev.mark(), w1.dev.mark()
        w1.dev.send("?MGMT,PULL,2")
        _stamp(w1.dev, 2)
        w1.dev.expect(r"^\[MGMT:CONFIG,2\]", timeout=5, since=m1r)
        time.sleep(1.0)
        between = [x for _, x in list(w2.dev.lines[md:mr])]
        if not any(x.startswith("[MGMT] Duplicate config request from WCB1 ignored") for x in between):
            problems.append("W2 did not log the 1.0 s re-send as a duplicate")
        if any(re.search(ACCEPTED_W1, x) for x in between):
            problems.append(f"W2 took the pull re-sent 1.0 s after it took the first (the window is {DEDUP_S} s)")
        if not any(re.search(ACCEPTED_W1, x) for x in w2.dev.since(mr)):
            problems.append("W2 did not take the pull re-sent 1.7 s after the first")
        seen = _pull_lines(w1.dev, m1, 2)
        if seen["order"] != ["CONFIG", "CONFIG"]:
            problems.append(f"W1 printed {seen['order']} for three pulls; expected two config lines, the 1.0 s "
                            f"re-send unanswered")
        col = PullCollector(2, m1r)
        for x in w1.dev.since(m1r):
            if col.feed(x) == "complete":
                break
        if col.reply is None:
            problems.append("the reply to the 1.7 s re-send is not a whole config line")
        else:
            r = col.result()
            if r.provided != r.calc:
                problems.append(f"the reply to the 1.7 s re-send ({len(r.text)} characters) fails its CRC")
        bench.note(f"W2 took the first pull; re-sends at +1.0 s and +1.7 s; W1 printed {seen['order']}")
    finally:
        w2.run("?DEBUG,MGMT,OFF")
    assert not problems, "; ".join(problems)


@test("wcb.pull_park_second_requester", "Two relays pull W2 within 100 ms (W1 and NaviCore, ?MGMT,PULL,2,P): W2 serves one, parks the other behind it ('parked behind' under ?DEBUG,MGMT) and serves it next; both get W2's whole config, CRC-valid, and W1 prints only its own reply, not the one W2 then sends NaviCore (WCB-WP25)", needs=["wcb1", "wcb2", "navicore"])
def pull_park_second_requester(bench):
    """A request from another requester while a job runs is parked, then served oldest first when the job ends
    (handleConfigReqPacket, cpjEnd; WCB.ino), and a relay prints only the frags whose requesterWCB is its own
    (handleConfigFragPacket). W2 is grown to just under 2912 characters - a 16-frag one-line reply of about 0.6 s - so
    the second request lands while the first job runs, and a NaviCore on a library from before F13 (which never asks
    for parts and drops packet type 18) is answered like a new one. The throwaway HILP sequences are removed again."""
    w1, w2, nc = usb_wcb(bench), _w2(bench), bench.dev("navicore")
    keys, problems = [], []
    with config_guard(bench, 2):
        try:
            ver = w2.version()
            n = _grow(w2, ver, keys, PULL_MAX - 300)
            assert n <= PULL_MAX, f"W2's config came to {n} characters, over the one-line limit"
            want = _factory_reply(w2, ver)
            _knob(w2, "?DEBUG,MGMT,ON")
            m1, m2, mn = w1.dev.mark(), w2.dev.mark(), nc.mark()
            pulls = {"W1": Pull(w1.dev, 2, parts=True, timeout=10)}
            t0 = time.monotonic()
            pulls["NaviCore"] = Pull(nc, 2, parts=True, timeout=10)
            gap = time.monotonic() - t0
            done = {}
            while len(done) < len(pulls):
                for name, p in pulls.items():
                    if name in done:
                        continue
                    try:
                        if p.poll():
                            done[name] = p.reply(verify=False)
                    except AssertionError as e:
                        done[name] = e
                time.sleep(0.05)
            for name, r in done.items():
                if isinstance(r, Exception):
                    problems.append(f"{name}'s pull: {r}")
                else:
                    problems += [f"{name}'s pull: {x}" for x in _reply_problems(r, want, keys, "legacy", 1)]
            time.sleep(1.5)                     # a reply printed for the other relay would be in by now
            w2l = w2.dev.since(m2)
            parked = [x for x in w2l if re.search(r"^\[MGMT\] Config request from WCB(1|20) parked behind WCB(1|20)'s "
                                                  r"reply$", x)]
            taken = {m.group(1) for m in (re.search(r"^\[MGMT\] Config request from WCB(1|20)(?: \(parts accepted\))?$",
                                                    x) for x in w2l) if m}
            if not parked:
                problems.append(f"W2 logged no 'parked behind' for two pulls sent {gap * 1000:.0f} ms apart")
            if taken != {"1", "20"}:
                problems.append(f"W2 logged taking pulls from WCB{sorted(taken)}; expected both WCB1 and WCB20")
            ours = _pull_lines(w1.dev, m1, 2)["CONFIG"]
            if len(ours) != 1:
                problems.append(f"W1 printed {len(ours)} [MGMT:CONFIG,2] lines; one is its own, a second is the reply "
                                f"W2 sent NaviCore")
            theirs = _pull_lines(nc, mn, 2)["CONFIG"]
            bench.note(f"W2 at {n} characters; pulls sent {gap * 1000:.0f} ms apart; W2: {len(parked)} parked line(s), "
                       f"took WCB{sorted(taken)}; config lines: W1 {len(ours)}, NaviCore {len(theirs)}")
        finally:
            try:
                w2.run("?DEBUG,MGMT,OFF")
            finally:
                _clear(w2, keys)
    assert not problems, "; ".join(problems)


@test("wcb.chain_partial_tokens", "?PEERSLIVE rides only the factory chain (once), never the configured one; a custom controller id outlives a disable - ?CONTROLLER,ON,17 then ?CONTROLLER,OFF leaves '?CONTROLLER,ON,17^?CONTROLLER,OFF' in the chain - and W1's own controller line puts it back (auto-join off meanwhile; WCB-WP25)", needs=["wcb1"])
def chain_partial_tokens(bench):
    """collectConfigCommands (WCB.ino) emits PEERSLIVE with includeInLive=false, which printBackupConfig leaves out of
    the configured chain, and emits a disabled controller only when its id is not the default 20, as ON,<id> then OFF.
    With the controller off, a WDP advert from NaviCore would auto-join it as a persisted learned peer (the auto-join
    block in WCB_WDP.cpp), so ?WDP,AUTOJOIN is off for the few seconds it is. Id 17 must be free on W1."""
    w = usb_wcb(bench)
    problems = []
    chains = backup_chain_lines(w.run("?backup", timeout=10))
    for name, want in (("configured", 0), ("factory", 1)):
        got = [t for t in (chains[name] or "").split("^") if t.upper().startswith("?PEERSLIVE,")]
        if len(got) != want:
            problems.append(f"the {name} chain holds {len(got)} ?PEERSLIVE token(s), expected {want}")
    if any(x.startswith("[WDP:N=17,") for x in w.run("?WDP,DUMP", timeout=8)):
        raise Skip("W1 knows a device 17, so it cannot stand in for a controller id nobody uses")
    with config_guard(bench, 1) as before:
        own = [t for t in before[1] if t.upper().startswith("?CONTROLLER,")] or ["?CONTROLLER,ON,20", "?CONTROLLER,OFF"]
        joined = "?WDP,AUTOJOIN,OFF" not in before[1]
        try:
            if joined and not any(x.startswith("[WDP] auto-join disabled") for x in w.run("?WDP,AUTOJOIN,OFF")):
                problems.append("?WDP,AUTOJOIN,OFF did not confirm")
            out = [x.rstrip() for x in w.run("?CONTROLLER,ON,17")]
            if "Controller peer ID set to 17." not in out:
                problems.append(f"?CONTROLLER,ON,17 printed {out}")
            out = [x.rstrip() for x in w.run("?CONTROLLER,OFF")]
            if "Controller peer (ID 17) DISABLED." not in out:
                problems.append(f"?CONTROLLER,OFF printed {out}")
            toks = w.backup_chain()[0]
            i = next((k for k, t in enumerate(toks) if t == "?CONTROLLER,ON,17"), None)
            if i is None or toks[i + 1:i + 2] != ["?CONTROLLER,OFF"]:
                problems.append(f"the chain's controller tokens are {[t for t in toks if t.startswith('?CONTROLLER')]}, "
                                f"expected ?CONTROLLER,ON,17 then ?CONTROLLER,OFF")
        finally:
            for t in own:
                w.run(t)
            if joined:
                w.run("?WDP,AUTOJOIN,ON")
    assert not problems, "; ".join(problems)


PULLPART_BAD = "Invalid PULLPART size. Use 512-2880, or 0/OFF for the default (2880)"


@test("wcb.pull_knobs", "?DEBUG edge forms on W2: KYBER,ON/OFF is the Maestro switch; an unknown ?DEBUG,<x> and a PULLPART of 511, 2881 or abc are refused; PULLPART,0 restores 2880; PULLFAULT,OFF disarms an armed fault, and an armed fault lapses unfired after 60 s - both followed by a whole pull (WCB-WP25; ~70 s)", needs=["wcb1", "wcb2"])
def pull_knobs(bench):
    """The ?DEBUG handler (WCB.ino) takes KYBER as an alias of MAESTRO; PULLPART accepts a number only when its text
    is the number itself (v == String(req)), and cfgpPartDataSize (WCB_ConfigParts.h) maps 0 to 2880 and anything
    outside 512-2880 to 0 = refused. configPullFaultArm stamps the arm time and cpjStart fires the fault only within
    CPJ_FAULT_ARM_MS (60 s); either way the next accepted request disarms it. All RAM only."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    try:
        for cmd, want in (("?DEBUG,KYBER,ON", "Maestro debugging enabled"),
                          ("?DEBUG,KYBER,OFF", "Maestro debugging disabled"),
                          ("?DEBUG,HILNOPE", "Invalid DEBUG command. Use: ?DEBUG ?"),
                          ("?DEBUG,PULLPART,511", PULLPART_BAD), ("?DEBUG,PULLPART,2881", PULLPART_BAD),
                          ("?DEBUG,PULLPART,abc", PULLPART_BAD),
                          ("?DEBUG,PULLPART,0", "Config pull part size: 2880 bytes (default)"),
                          ("?DEBUG,PULLFAULT,OOM", "Config pull fault armed: the next accepted config request fails its "
                                                   "reply buffer allocation (one-shot; disarms after firing or in 60 s)"),
                          ("?DEBUG,PULLFAULT,OFF", "Config pull fault disarmed")):
            out = [x.rstrip() for x in w2.run(cmd)]
            if not any(x.startswith(want) for x in out):      # println: another task's line can glue onto its end
                problems.append(f"{cmd} printed {out[:3]}, not {want!r}")
        for when in ("after PULLFAULT,OFF", "61 s after PULLFAULT,OOM"):
            if when.startswith("61"):
                _knob(w2, "?DEBUG,PULLFAULT,OOM")
                time.sleep(61)
            try:
                r = w1.pull_reply(2, timeout=10, verify=False)
                if r.provided != r.calc:
                    problems.append(f"the pull {when} ({len(r.text)} characters) fails its CRC")
            except PullRefused as e:
                problems.append(f"the pull {when} was refused {e.code}: the fault fired")
    finally:
        for cmd in ("?DEBUG,PULLFAULT,OFF", "?DEBUG,PULLPART,OFF", "?DEBUG,MAESTRO,OFF"):
            w2.run(cmd)
    assert not problems, "; ".join(problems)


@test("wcb.mgmt_result_too_large", "A ?MGMT,STATS reply over 16 x 182 characters is refused in words: with 20 made-up ?STATS,RPT rows on W2, W1 prints [MGMT:STATS,2][ERROR] Result too large to relay (...) and W2 prints its own '[MGMT] Result too large to relay:' line; ?STATS,RESET clears the rows (WCB-WP25)", needs=["wcb1", "wcb2"])
def mgmt_result_too_large(bench):
    """sendResultFrags (WCB.ino) refuses a STATS, SEQ-names or ETM result longer than MGMT_MAX_CHUNKS x 182 with an
    ungated line and a short '[ERROR] Result too large to relay (<n> chars, max 2912). ...' sent in its place. A row
    storeReportedStats keeps prints about 156 characters with seven 10-digit counters, so 20 of them pass 2912 even if
    NaviCore's own 30 s report overwrites one. RAM only: resetESPNowStats clears the reported rows too."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    try:
        for k in range(1, 21):
            w2.run(f"?STATS,RPT,{k}" + ",4294967295" * 7)
        rows = [x for x in w2.run("?STATS", timeout=6) if re.match(r"^WCB\d+: Sent: 4294967295, .*Unguaranteed", x)]
        if len(rows) < 19:
            problems.append(f"W2's ?STATS shows {len(rows)} of the 20 reported rows")
        line = None
        for _ in range(2):                      # sent once and unacknowledged (handleMgmtStatsRequest): ask twice
            m1, m2 = w1.dev.mark(), w2.dev.mark()
            w1.dev.send("?MGMT,STATS,2")
            try:
                line = w1.dev.expect(r"^\[MGMT:STATS,2\].*", timeout=5, since=m1).group(0)
                break
            except AssertionError:
                continue
        if line is None:
            problems.append("W1 printed no [MGMT:STATS,2] line for two requests")
        elif not re.match(r"^\[MGMT:STATS,2\]\[ERROR\] Result too large to relay \(\d+ chars, max 2912\)\.", line):
            problems.append(f"W1's [MGMT:STATS,2] line is not the too-large error: {line[:100]!r}")
        if line is not None and not any(re.match(r"^\[MGMT\] Result too large to relay: \d+ chars needs \d+ chunks, "
                                                 r"max 16 \(2912 chars\)\.", x) for x in w2.dev.since(m2)):
            problems.append("W2 did not print '[MGMT] Result too large to relay:'")
    finally:
        w2.run("?STATS,RESET")
    left = [x for x in w2.run("?STATS", timeout=6) if "4294967295" in x]
    assert not left, f"?STATS,RESET left {len(left)} made-up row(s) on W2"
    assert not problems, "; ".join(problems)


@test("wcb.pull_wrong_target", "?MGMT,PULL,3,P - addressed to a board the bench does not have - is dropped by W2 before its dedup (no 'Config request' or 'Duplicate' line under ?DEBUG,MGMT) and brings W1 no pull line; the same pull of W2 is then taken and answered (WCB-WP25)", needs=["wcb1", "wcb2"])
def pull_wrong_target(bench):
    """handleConfigReqPacket (WCB.ino) returns on targetWCB != WCB_Number before anything else is logged; the relay
    (handleMgmtPullRequest) accepts any target 1-20, so the request does go out. Read-only apart from ?DEBUG,MGMT."""
    if 3 in bench.wcb_numbers():
        raise Skip("WCB3 is on this bench")
    w1, w2 = usb_wcb(bench), _w2(bench)
    problems = []
    try:
        _knob(w2, "?DEBUG,MGMT,ON")
        m1, m2 = w1.dev.mark(), w2.dev.mark()
        w1.dev.send("?MGMT,PULL,3,P")
        time.sleep(3.0)
        logged = [x for x in w2.dev.since(m2) if re.match(r"^\[MGMT\] (Config request|Duplicate config request) from "
                                                         r"WCB1", x)]
        if logged:
            problems.append(f"W2 logged {len(logged)} config request line(s) for a pull addressed to WCB3")
        stray = [x[:24] for x in w1.dev.since(m1) if parse_pull_line(x)]
        if stray:
            problems.append(f"W1 printed pull lines for a pull of WCB3: {stray}")
        m2 = w2.dev.mark()
        w1.pull_reply(2, timeout=10, verify=False)
        if not any(re.search(ACCEPTED_W1, x) for x in w2.dev.since(m2)):
            problems.append("W2 logged no 'Config request from WCB1' for a pull of W2 either: ?DEBUG,MGMT did not log")
    finally:
        w2.run("?DEBUG,MGMT,OFF")
    assert not problems, "; ".join(problems)


@test("wcb.pull_session_reaped", "A one-line pull session W1 is left holding half of (W1 deafened for 2 s after its first frags) is reaped 10 s after its last frag with '[MGMT] Config pull session <id> timed out' under ?DEBUG,MGMT, and the next pull of W2 arrives whole (WCB-WP25)", needs=["wcb1", "wcb2"])
def pull_session_reaped(bench):
    """checkConfigPullTimeout (WCB.ino) drops a relay's pull session CONFIG_SESSION_TIMEOUT_MS (10 s) after its last
    frag. W2 is grown to about 2.7 KB, a 15-frag reply. '?MAC,3,<other>' changes W1's receive filter at once, but not
    its radio address, which moves only at boot (the trick of s18's _deaf_w1; the octet is saved, so it is put back).
    W1 deafens ITSELF, from a timer chain sent on the line right behind the pull:
    ;S0<m>^;T<d>^?MAC,3,<other>^;T2000^?MAC,3,<orig>. The whole reply lands within one host read about 0.6 s after
    the pull, so a ?MAC,3 the host sent on seeing the session line always came after the last frag (run
    20260928-064402); a ?MGMT chain is never a timer chain (isTimerChain), so the delay cannot ride the pull's own
    line; and a ?SEQ,SAVE value ends at '^?', so it cannot be a stored sequence. The chain also puts the octet back on
    W1's clock, whatever happens to the host. The delay steps across tries until one lands inside the reply. The
    SEQ-names half of the plan row is not here: W2's name list fits one frag, which is never left half-received."""
    w1, w2 = usb_wcb(bench), _w2(bench)
    keys, problems = [], []
    with config_guard(bench, 1, 2) as before:
        t = token(before[1], "?MAC,3,")
        if t is None:
            raise Skip("W1's chain lacks ?MAC,3")
        orig = t[len("?MAC,3,"):]
        other = "%02X" % (int(orig, 16) ^ 0x01)
        deaf = False
        try:
            ver = w2.version()
            n = _grow(w2, ver, keys, PULL_MAX - 200)
            assert n <= PULL_MAX, f"W2's config came to {n} characters, over the one-line limit"
            want = _factory_reply(w2, ver)
            w1.run("?DEBUG,MGMT,ON")
            reaped = None
            for attempt, delay in enumerate((100, 200, 300, 400, 500, 650), 1):
                mark = marker("d")
                m1 = w1.dev.mark()
                w1.dev.send("?MGMT,PULL,2")
                w1.dev.send(f";S0{mark}^;T{delay}^?MAC,3,{other}^;T2000^?MAC,3,{orig}")
                _stamp(w1.dev, 2)
                deaf = True
                w1.dev.expect(rf"Updated 3rd MAC octet to 0x{orig}", timeout=delay / 1000 + 6, since=m1)
                deaf = False
                lines = list(w1.dev.lines[m1:])
                if not any(f"Updated 3rd MAC octet to 0x{other}" in x for _, x in lines):
                    problems.append(f"try {attempt}: W1 never deafened (no ?MAC,3,{other} line)")
                    break
                g = next((re.search(r"^\[MGMT\] Config pull session ([0-9A-F]{4}) from WCB2 \(\d+ chunks\)", x)
                          for _, x in lines if x.startswith("[MGMT] Config pull session ")), None)
                if not g:
                    bench.note(f"try {attempt}: W1 deafened ;T{delay} before the first frag")
                    time.sleep(2.0)
                    continue
                sid = g.group(1)
                frags = [ts for ts, x in lines if re.search(rf"Config frag \d+/\d+ received \(session {sid}\)", x)]
                if any(x.startswith("[MGMT:CONFIG,2]") for _, x in lines):
                    bench.note(f"try {attempt}: W1 deafened ;T{delay} after the last frag - session {sid} completed")
                    time.sleep(2.0)
                    continue
                w1.dev.expect(rf"^\[MGMT\] Config pull session {sid} timed out", timeout=14, since=m1)
                t_out, _ = _at(w1.dev, m1, rf"^\[MGMT\] Config pull session {sid} timed out")
                reaped = (len(frags), round(t_out - frags[-1], 1) if frags else None)
                if frags and not 9.5 <= t_out - frags[-1] <= 11.5:
                    problems.append(f"session {sid} was reaped {t_out - frags[-1]:.1f} s after its last frag, not ~10 s")
                break
            if reaped is None and not problems:
                problems.append("no try deafened W1 inside the reply: no half-received session to reap")
            bench.note(f"W2 at {n} characters; reaped session: (frags received, seconds from the last to the "
                       f"timeout line) {reaped}")
            r = w1.pull_reply(2, timeout=10, verify=False)
            problems += [f"the pull after the reaping: {x}" for x in _reply_problems(r, want, keys, "legacy", 1)]
        finally:
            try:
                if deaf:
                    w1.run(f"?MAC,3,{orig}")
                w1.run("?DEBUG,MGMT,OFF")
            finally:
                _clear(w2, keys)
    assert not problems, "; ".join(problems)
