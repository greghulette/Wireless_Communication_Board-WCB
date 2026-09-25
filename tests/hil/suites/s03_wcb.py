"""WCB console basics — config integrity and command parsing, and the mesh config pull at size (F13). Changes nothing,
except the size and pull tests, which add throwaway sequences (HILB../HILP..) or arm a RAM-only ?DEBUG knob on W2 and
remove them again."""
import math
import re
import time

from hil.runner import test
from hil.wcb import (CONFIG_TEXT, PULL_MAX, WCB, Pull, PullRefused, chain_crc, comparable, dev_note, group_tokens,
                     parse_error, parse_part, parse_pull_line, screen_code)
from suites.common import config_guard, remote_wcbs, usb_wcb

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
