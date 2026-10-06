"""Stored sequences (?SEQ, ;C/;SEQ recall, legacy ?CS/?CE) and the sequence inventory relay (?MGMT,SEQ / SEQGET).

Built from the vars_seq_inventory specs; the console literals were re-checked against WCB.ino, WCB_Storage.cpp and
WCB_WDP.cpp. Rules from the specs:
- A top-level ;C<key> or ;SEQ<key> without ',L' ETM-broadcasts the recall to W2 and NaviCore (WCB.ino:6716-6719), so
  keys are HIL-prefixed and W2 sequences never write W2 S1 (its real Maestro).
- ';W2,?SEQ,SAVE,k,a^b' is split by the sender (W2 stores 'a', 'b' runs on W1, WCB.ino:2367-2389): values with '^'
  go to W2 through ?MGMT,FRAG.
- ?SEQ,CLEAR,ALL / ?CCLEAR wipe every sequence; that test is opt-in (bench.json "opt_in": ["seq_wipe"]) and replays
  the snapshot's ?SEQ,SAVE tokens in order, since save order drives both backup order and the SEQHASH.
- Sequences live in their own store, a file (WCB_SeqStore.h), since 2026-09-29; NVS keeps only the stored_cmds
  marker seq_mig_done. ?SEQ,GET answers NOTFOUND for the NVS layout's records, so ?NVS is the window onto them.
- A target answers one SEQ_REQ per requester, and one SEQVAL_REQ per (requester, last key), per 1500 ms
  (WCB.ino:3593-3601, 3634-3646): identical relay requests are spaced 1.7 s apart.
- The mesh-client specs (seq.fanout_wire_probe, seq.peer_body_broadcasts) live with the probe mesh-mode tests.
"""
import re
import time

from hil.nvs import parse as nvs_parse
from hil.runner import Skip, test
from hil.wcb import PULL_SPACING_S, WCB, PullCollector, PullRefused, chain_crc, pull_config
from suites.common import Console, config_guard, link, marker, nonce, snapshot, token, usb_wcb
from suites.s03_wcb import backup_chain_problems

NAMES_RX = re.compile(r"^\[MGMT:SEQ,(\d+)\]([0-9A-F]{8}),(\d+)((?:,[^,]+)*)$")


def _has(lines, text):
    return any(text in x for x in lines)


def _parse_names(line):
    """'[MGMT:SEQ,n]HASH,count,names...' -> (hash, count, [names]); the count must match the names."""
    m = NAMES_RX.match(line.rstrip())
    if not m:
        raise AssertionError(f"malformed inventory line: {line!r}")
    names = [x for x in m.group(4).split(",") if x]
    if int(m.group(3)) != len(names):
        raise AssertionError(f"inventory count {m.group(3)} but {len(names)} names: {line!r}")
    return m.group(2), int(m.group(3)), names


def _names(w):
    return _parse_names(next(x for x in w.run("?SEQ,NAMES") if x.startswith("[MGMT:SEQ,")))


def _seqval(w, key):
    return next((x.rstrip("\r\n") for x in w.run(f"?SEQ,GET,{key}") if x.startswith("[MGMT:SEQVAL,")), None)


def _stored_cmds(w):
    """Entries the stored_cmds NVS namespace uses (?NVS), or None on firmware without ?NVS. With the sequence store on
    its file that is 1 - seq_mig_done - once no fallback boot or older firmware left sequences there."""
    stats, spaces = nvs_parse(w.run("?NVS", timeout=6))
    return None if stats is None else spaces.get("stored_cmds", 0)


def _inventory_hash(strings):
    """FNV-1a 32 over the key list and then each value, each followed by a 0xFF separator (seqStoreHash,
    WCB_SeqStore.cpp)."""
    h = 0x811C9DC5
    for s in strings:
        for b in s.encode("utf-8"):
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
        h = ((h ^ 0xFF) * 16777619) & 0xFFFFFFFF
    return "%08X" % h


def _canonical(w, names):
    """The key list is 'a,b,' exactly, so a clear and replay reproduces the hash. The sequence store builds it from its
    records, so it always is (?SEQ,GET,key_list is NOTFOUND there); in the NVS layout a migration or a hand edit
    could leave key_list irregular, and older firmware reads it back through ?SEQ,GET."""
    kl = _seqval(w, "key_list") or ""
    if kl.endswith("key_list,NOTFOUND,"):
        return True
    return kl == "[MGMT:SEQVAL,1]key_list,OK," + "".join(f"{n}," for n in names)


def _clear_seq(w, *keys):
    for k in keys:
        w.run(f"?SEQ,CLEAR,{k}")


def _nvs_refused(out):
    """A save the store refused for want of room (saveStoredCommandsToPreferences, WCB_Storage.cpp): the sequence
    store full, or - on the NVS layout - NVS full or fragmented. A setup it cannot store is a Skip naming the cause,
    not a failure (as in seq.top_level_too_big_refused)."""
    return (_has(out, "NVS write rejected") or _has(out, "NVS could not update the sequence list")
            or _has(out, "the sequence store is full"))


def _save_or_skip(w, key, value):
    """?SEQ,SAVE a setup sequence, or Skip when NVS refuses it."""
    out = w.run(f"?SEQ,SAVE,{key},{value}", timeout=8)
    if _nvs_refused(out):
        raise Skip(f"W1's NVS refused the {len(value)}-character setup sequence {key} (see ?NVS)")
    if not _has(out, f"Stored: Key='{key}'"):
        raise AssertionError(f"setup: {key} was not stored: {out[:3]}")


# ============================================================ ?SEQ commands
@test("seq.command_formats", "?SEQ SAVE/GET/CLEAR success and error literals; unknown subcommand", needs=["wcb1"], links=[])
def command_formats(bench):
    w = usb_wcb(bench)
    a, b = marker("a"), marker("b")
    value = f";S1{a}^;S1{b},x"
    fmt, usage = "Invalid format. Use ?Ckey,value", "Usage: ?SEQ,GET,<key>"
    checks = [(f"?SEQ,SAVE,HILK1,{value}", f"Stored: Key='HILK1', Value='{value}'"), ("?SEQ,SAVE,HILK1", fmt), ("?SEQ,SAVE", fmt),
              ("?SEQ,SAVE,,x", fmt), ("?SEQ,SAVE,HILK2,", "Key or value cannot be empty."),
              ("?SEQ,GET,HILK1", f"[MGMT:SEQVAL,1]HILK1,OK,{value}"), ("?SEQ,GET,hilk1", "[MGMT:SEQVAL,1]hilk1,NOTFOUND,"),
              ("?SEQ,GET,HILNOPE", "[MGMT:SEQVAL,1]HILNOPE,NOTFOUND,"), ("?SEQ,GET", usage), ("?SEQ,GET,", usage),
              ("?SEQ,CLEAR,HILK1", "Deleted stored command key: 'HILK1'"),
              ("?SEQ,CLEAR,HILK1", "No stored value found for key: 'HILK1', but removed from list if present."),
              ("?SEQ,CLEAR,", "Command name cannot be empty."), ("?SEQ,BOGUS", "Invalid SEQ command. Use: ?SEQ ?"),
              ("?SEQ", "Invalid SEQ command. Use: ?SEQ ?")]
    with config_guard(bench, 1):
        try:
            bad = [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
        finally:
            _clear_seq(w, "HILK1", "HILK2")
    assert not bad, "; ".join(bad)


@test("seq.key_len_15", "Keys: 15 characters store and recall; 16 are refused by ?SEQ,SAVE and legacy ?CS and change nothing", needs=["wcb1"])
def key_len_15(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    k15, k16 = "HILABCDEFGHIJKL", "HILABCDEFGHIJKLM"
    a, b = marker("a"), marker("b")
    bad = []
    with config_guard(bench, 1):
        try:
            if not _has(w.run(f"?SEQ,SAVE,{k15},;S1{a}"), f"Stored: Key='{k15}', Value=';S1{a}'"):
                bad.append("the 15-character key was not stored")
            pm = s1.mark()
            w.send(f";C{k15},L")
            try:
                s1.expect(a.encode() + b"\r", timeout=2, since=pm)
            except AssertionError:
                bad.append("the 15-character key did not recall")
            h1 = _names(w)
            for cmd in (f"?SEQ,SAVE,{k16},;S1{b}", f"?CS{k16},;S1{b}"):
                out = w.run(cmd)
                if not any(k16 in x and "is 16 characters" in x and "the limit is 15. Not stored." in x for x in out):
                    bad.append(f"{cmd[:10]}...: {out}")
            if _names(w) != h1:
                bad.append("a refused 16-character save changed the inventory")
            if _seqval(w, k16) != f"[MGMT:SEQVAL,1]{k16},NOTFOUND,":
                bad.append("the 16-character key reads back")
            pm = s1.mark()
            if not _has(w.run(f";C{k16},L"), f"No command stored under key: '{k16}'"):
                bad.append("recalling the 16-character key did not report it missing")
            time.sleep(0.5)
            if b.encode() in s1.received(pm):
                bad.append("the refused sequence ran")
        finally:
            _clear_seq(w, k15)
    assert not bad, "; ".join(bad)


@test("seq.clear_overlong_key_keeps_prefix", "?SEQ,CLEAR and legacy ?CE with a 16-character key leave the sequence stored under its 15-character prefix: each says 'No stored value found', the inventory is unchanged, and the 15-character key still reads back and recalls", needs=["wcb1"])
def clear_overlong_key_keeps_prefix(bench):
    """WCB-WP55 row 2. NVS compares only 15 characters of a key, so removing by a 16-character one would delete the
    15-character key's value. eraseStoredCommandByName never removes by a key over SEQ_KEY_MAX_LEN (WCB_Storage.cpp:
    854-858) and only rewrites key_list, where the longer name matches nothing (:860-889), then says it found no value
    (:898-902). ?SEQ,CLEAR (WCB.ino:6773-6781) and ?CE (:6985-6986 -> :7242-7244) both land there. seq.key_len_15 covers
    SAVE, GET and recall with the long key."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    k15, k16 = "HILABCDEFGHIJKL", "HILABCDEFGHIJKLM"
    a = marker("a")
    bad = []
    with config_guard(bench, 1):
        try:
            _save_or_skip(w, k15, f";S1{a}")
            inv = _names(w)
            for cmd in (f"?SEQ,CLEAR,{k16}", f"?CE{k16}"):
                out = w.run(cmd)
                if not _has(out, f"No stored value found for key: '{k16}', but removed from list if present."):
                    bad.append(f"{cmd} printed {out}")
            if _seqval(w, k15) != f"[MGMT:SEQVAL,1]{k15},OK,;S1{a}":
                bad.append(f"after the clears the 15-character key reads {_seqval(w, k15)!r}")
            if _names(w) != inv:
                bad.append(f"the clears changed the inventory: {_names(w)}, before {inv}")
            pm = s1.mark()
            w.send(f";C{k15},L")
            try:
                s1.expect(a.encode() + b"\r", timeout=2, since=pm)
            except AssertionError:
                bad.append("the 15-character key no longer recalls")
        finally:
            _clear_seq(w, k15)
    assert not bad, "; ".join(bad)


@test("seq.list_format","?SEQ,LIST header, rows in save order, footer", needs=["wcb1"], links=[])
def list_format(bench):
    w = usb_wcb(bench)
    a, b, c = marker("a"), marker("b"), marker("c")
    with config_guard(bench, 1):
        _clear_seq(w, "HILL1", "HILL2")
        try:
            w.run(f"?SEQ,SAVE,HILL1,;S1{a}^;S1{b}")
            w.run(f"?SEQ,SAVE,HILL2,{c} text")
            lst = [x.rstrip() for x in w.run("?SEQ,LIST")]
        finally:
            _clear_seq(w, "HILL1", "HILL2")
    r1, r2 = f"Key: 'HILL1' -> Value: ';S1{a}^;S1{b}'", f"Key: 'HILL2' -> Value: '{c} text'"
    assert "--- Stored Commands ---" in lst, lst[:5]
    assert r1 in lst and r2 in lst and lst.index(r1) < lst.index(r2), f"rows missing or out of order: {lst}"
    assert [x for x in lst if x.startswith("---")][-1] == "--- End of Stored Commands ---", lst[-3:]


def _config_section(lines, header):
    """The lines under `header` in a ?config printout for as long as they are indented deeper than it, or None when the
    header is missing. ?config prints the mesh password, so a test quotes a section, never the whole printout."""
    lines = [x.rstrip() for x in lines]
    if header not in lines:
        return None
    depth = len(header) - len(header.lstrip())
    out = []
    for x in lines[lines.index(header) + 1:]:
        if not x.strip() or len(x) - len(x.lstrip()) <= depth:
            break
        out.append(x)
    return out


@test("seq.config_lists_sequences", "?config's 'Stored Sequences:' section lists each sequence as '  <key> = <value>' and drops it once cleared; its 'Serial Monitoring:' section reads None, since no command sets it", needs=["wcb1"], links=[])
def config_lists_sequences(bench):
    """WCB-WP55 row 4. printConfigInfo walks key_list and prints '  <key> = <value>' per sequence, or '  None' when none
    is stored (WCB.ino:3006-3028). Its 'Serial Monitoring:' block names a port only when serialMonitorEnabled is set
    (:2931-2945), which only loadSerialMonitorSettings writes, at boot, from the serial_monitor namespace
    (WCB_Storage.cpp:2288-2297) - and nothing ever calls saveSerialMonitorSettings (:2054), so only an older firmware's
    keys could make it name a port: that part is checked only while ?NVS lists no serial_monitor namespace (the dead
    branch is WCB-WP59 row 14). ?config prints the mesh password, so only these two sections are quoted."""
    w = usb_wcb(bench)
    value = ";S0x"
    with config_guard(bench, 1):
        _clear_seq(w, "HILCF")
        try:
            _save_or_skip(w, "HILCF", value)
            cfg = w.run("?config", timeout=8)
            listed, monitoring = _config_section(cfg, "Stored Sequences:"), _config_section(cfg, "  Serial Monitoring:")
            _clear_seq(w, "HILCF")
            after = _config_section(w.run("?config", timeout=8), "Stored Sequences:")
            stats, spaces = nvs_parse(w.run("?NVS", timeout=6))
        finally:
            _clear_seq(w, "HILCF")
    assert listed is not None and f"  HILCF = {value}" in listed, f"'Stored Sequences:' with HILCF saved: {listed}"
    assert after is not None and not any(x.startswith("  HILCF = ") for x in after), \
        f"'Stored Sequences:' after ?SEQ,CLEAR,HILCF: {after}"
    if stats and spaces.get("serial_monitor"):
        bench.note(f"W1's NVS holds serial_monitor keys from older firmware; 'Serial Monitoring:' reads {monitoring}")
    else:
        assert monitoring == ["    None"], f"'Serial Monitoring:' reads {monitoring}, though nothing sets it"


@test("seq.clear_all_empty_hash","OPT-IN (seq_wipe): ?SEQ,CLEAR,ALL and ?CCLEAR wipe W1's sequences and each re-stamps seq_mig_done (stored_cmds keeps exactly that one entry); an empty inventory hashes to 7A0B824E, not the documented 811C9DC5", needs=["wcb1"], links=[], opt_in="seq_wipe")
def clear_all_empty_hash(bench):
    """Doc bug: docs/SEQUENCE_INVENTORY.md:137 and :249, docs/WDP_DESIGN.md:109, WCB_Storage.h:164, WCB_WDP.h:89,
    tests/wdp_wire_test.cpp:69 and WCBClient (WCB_Client.h:404, README.md:536) give the empty hash as 811C9DC5, but
    sequenceInventoryHash() applies the 0xFF separator even to an empty key_list (WCB_Storage.cpp:792-798).
    WCB-WP44 row 2: preferences.clear() also removes seq_mig_done, which lives in the same namespace, so the clear
    writes it back at once (nvsClear, WCB_SeqStore.cpp); without it the legacy migration would re-import the old
    CMD1..CMD80 sequences at the next boot, and a boot would take the sequence store's file for one a factory reset
    left behind. ?SEQ,GET no longer reads it; ?NVS shows stored_cmds holding that one entry and nothing else."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1):
        saved = [t for t in snapshot(bench, 1) if t.upper().startswith("?SEQ,SAVE,")]
        if [t for t in saved if "^?" in t or len(t) > 1000]:
            raise Skip("a stored value cannot be replayed from one USB line")
        h0, _, names0 = _names(w)
        canonical = _canonical(w, names0)
        try:
            if not _has(w.run("?SEQ,CLEAR,ALL"), "All stored sequences cleared"):
                problems.append("?SEQ,CLEAR,ALL did not confirm")
            if _stored_cmds(w) != 1:
                problems.append(f"after ?SEQ,CLEAR,ALL stored_cmds holds {_stored_cmds(w)} NVS entries, not 1 "
                                f"(seq_mig_done): without it the legacy migration would run again at the next boot")
            if "No stored commands." not in [x.rstrip() for x in w.run("?SEQ,LIST")]:
                problems.append("LIST is not empty")
            if _names(w) != ("7A0B824E", 0, []):
                problems.append(f"empty inventory {_names(w)}")
            if not _has(w.run("?WDP,DUMP", timeout=8), "[WDPSEQ:N=1,HASH=7A0B824E]"):
                problems.append("the WDP self record does not carry 7A0B824E")
            w.run("?SEQ,SAVE,HILA,;S1x")
            if _names(w) != ("0788F458", 1, ["HILA"]):
                problems.append(f"one sequence {_names(w)} (host FNV-1a gives 0788F458)")
            if not _has(w.run("?CCLEAR"), "All stored sequences cleared"):
                problems.append("?CCLEAR did not confirm")
            if _stored_cmds(w) != 1:
                problems.append(f"after ?CCLEAR stored_cmds holds {_stored_cmds(w)} NVS entries, not 1 (seq_mig_done)")
            if _names(w)[:2] != ("7A0B824E", 0):
                problems.append(f"after ?CCLEAR {_names(w)}")
        finally:
            _clear_seq(w, "HILA")
            for t in saved:
                w.run(t)
        if canonical and _names(w)[0] != h0:
            problems.append("the replay did not restore the original hash")
    assert _inventory_hash([""]) == "7A0B824E" and _inventory_hash(["HILA,", ";S1x"]) == "0788F458"
    assert not problems, "; ".join(problems)


@test("seq.names_order_hash", "?SEQ,NAMES lists in save order; an in-place edit changes only the hash; re-creating moves the name to the end", needs=["wcb1"], links=[])
def names_order_hash(bench):
    w = usb_wcb(bench)
    a, b, c = marker("a"), marker("b"), marker("c")
    bad = []
    with config_guard(bench, 1):
        _clear_seq(w, "HILN1", "HILN2")
        h0, n0, names0 = _names(w)
        canonical = _canonical(w, names0)
        try:
            w.run(f"?SEQ,SAVE,HILN1,{a}")
            w.run(f"?SEQ,SAVE,HILN2,{b}")
            h2, n2, names2 = _names(w)
            if n2 != n0 + 2 or names2[-2:] != ["HILN1", "HILN2"]:
                bad.append(f"after two saves {n2} {names2[-2:]}")
            w.run(f"?SEQ,SAVE,HILN1,{c}")
            h3, _, names3 = _names(w)
            if names3 != names2 or h3 == h2:
                bad.append("the in-place edit moved the name or kept the hash")
            w.run("?SEQ,CLEAR,HILN1")
            w.run(f"?SEQ,SAVE,HILN1,{c}")
            h4, _, names4 = _names(w)
            if names4[-2:] != ["HILN2", "HILN1"] or h4 in (h2, h3):
                bad.append(f"after clear and re-save {names4[-2:]}, hash repeated: {h4 in (h2, h3)}")
        finally:
            _clear_seq(w, "HILN1", "HILN2")
        h5, n5, _ = _names(w)
        if n5 != n0 or (canonical and h5 != h0):
            bad.append(f"after clearing both: count {n5} vs {n0}, hash {h5} vs {h0}")
    assert not bad, "; ".join(bad)


@test("seq.hash_algorithm", "?SEQ,NAMES hash == FNV-1a over the key list ('k1,k2,...,' in save order, NVS's key_list format) then each value, each followed by a 0xFF separator", needs=["wcb1"], links=[])
def hash_algorithm(bench):
    """The sequence store keeps NVS's key_list format and order (seqStoreHash, WCB_SeqStore.cpp), so peers holding a
    fingerprint from before the move see the same one after it. The list is built from ?SEQ,NAMES, which carries the
    names in store order."""
    w = usb_wcb(bench)
    a, b, c = marker("a"), marker("b"), marker("c")
    with config_guard(bench, 1):
        try:
            w.run(f"?SEQ,SAVE,HILH1,;S1{a}^;S1{b}")
            w.run(f"?SEQ,SAVE,HILH2,{c},x")
            h, _, names = _names(w)
            keys = "".join(f"{n}," for n in names)
            values = []
            for k in names:
                v = _seqval(w, k)
                values.append(v[len(f"[MGMT:SEQVAL,1]{k},OK,"):] if v and ",OK," in v else "")
        finally:
            _clear_seq(w, "HILH1", "HILH2")
    assert keys.endswith("HILH1,HILH2,"), f"key list {keys!r}"
    assert _inventory_hash([keys] + values) == h, f"W1 says {h}, host FNV-1a gives {_inventory_hash([keys] + values)}"


@test("seq.get_internal_keys", "Read-only: ?SEQ,GET answers NOTFOUND for the NVS layout's records key_list and seq_mig_done, and NAMES hides them", needs=["wcb1"], links=[])
def get_internal_keys(bench):
    """Until the sequence store moved to its own file (2026-09-29), ?SEQ,GET read these two NVS records back as if they
    were sequences - the only window onto them. They are no sequence in either layout, so GET refuses them like
    every other path (seqStoreGet, WCB_SeqStore.cpp); ?NVS shows the stored_cmds namespace instead. Recalling,
    saving or clearing them is refused too (seq.reserved_bookkeeping_keys)."""
    w = usb_wcb(bench)
    _, _, names = _names(w)
    kl, mig = _seqval(w, "key_list"), _seqval(w, "seq_mig_done")
    bench.note(f"stored_cmds NVS entries: {_stored_cmds(w)}")
    assert kl == "[MGMT:SEQVAL,1]key_list,NOTFOUND,", kl
    assert mig == "[MGMT:SEQVAL,1]seq_mig_done,NOTFOUND,", mig
    assert "key_list" not in names and "seq_mig_done" not in names, names


@test("seq.reserved_bookkeeping_keys", "The sequence store's own records (key_list, seq_mig_done) are refused as keys by ;C / ;SEQ, ?SEQ,SAVE and ?SEQ,CLEAR; the inventory and both records are unchanged (re-scan #14)", needs=["wcb1"], links=[])
def reserved_bookkeeping_keys(bench):
    """WCB coverage re-scan #14 (docs/hil_plan/WCB.md WCB-WP44 row 1). User keys live in "stored_cmds", the namespace
    that also holds key_list and seq_mig_done, and nothing reserved the two names: ?SEQ,SAVE,key_list,x corrupted the
    list, ?SEQ,CLEAR,key_list unlisted every sequence, ?SEQ,CLEAR,seq_mig_done re-armed the legacy migration, and
    ;Ckey_list ran the name list as a broadcast. seqKeyReserved (WCB_Storage.h) refuses both names now. The local recall
    goes first: on firmware without the guard it only broadcasts the names once, and the test stops before the arms
    that would damage the store - which is why this is not behind the seq_wipe opt-in."""
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1):
        inv = _names(w)
        records = _stored_cmds(w)
        out = w.run(";Ckey_list,L")
        if not _has(out, "'key_list' is a reserved name, not a sequence."):
            raise AssertionError(f";Ckey_list,L printed {out}: no reserved-name guard, so SAVE and CLEAR were not sent")
        not_seq = "'{}' is a reserved name, not a sequence."
        for line, text in ((";Ckey_list", not_seq.format("key_list")),
                           (";SEQseq_mig_done,L", not_seq.format("seq_mig_done")),
                           ("?SEQ,SAVE,key_list,HILX", "'key_list' is a reserved name (the sequence store keeps its own records under it). Not stored."),
                           ("?SEQ,SAVE,seq_mig_done,HILX", "'seq_mig_done' is a reserved name (the sequence store keeps its own records under it). Not stored."),
                           ("?SEQ,CLEAR,key_list", not_seq.format("key_list") + " Nothing cleared."),
                           ("?SEQ,CLEAR,seq_mig_done", not_seq.format("seq_mig_done") + " Nothing cleared.")):
            out = w.run(line)
            if not _has(out, text):
                problems.append(f"{line} printed {out}")
        if _names(w) != inv:
            problems.append(f"the inventory changed: {_names(w)}, before {inv}")
        if _stored_cmds(w) != records:
            problems.append(f"the stored_cmds NVS namespace went from {records} entries to {_stored_cmds(w)}")
    assert not problems, "; ".join(problems)


@test("seq.top_level_too_big_refused", "A top-level recall whose body cannot fit the command queue (210 commands, 200 slots) is refused whole with one line, and none of it runs (re-scan #24)", needs=["wcb1"], links=[])
def top_level_too_big_refused(bench):
    """WCB coverage re-scan #24 (docs/hil_plan/WCB.md WCB-WP55 row 1). The queue reserve that refuses a NESTED expansion
    whole did not apply at the top level (;C, ONFIN, ONERR), so a body longer than the free queue ran its first part,
    printed one "Command queue is full! Discarding command." line per dropped token - UART0 has no TX buffer - and lost
    the rest. Every token here is ;S0<one short tag>, so what ran is counted on USB. Recalled local-only (,L). The tag is
    2 characters, so the value is about 1.26 KB. With a full marker it was 2.9 KB, and the ?SEQ,SAVE line, copied several
    times while it is parsed, ran W1's ~18 KB AP-mode heap out (CLAUDE.md rule 14; run 20260928-004312). And an NVS string
    must fit in one 4 KB page: at 1.7 KB it needed about 54 contiguous free entries, which W1's pages no longer had late in
    full run 20260928-005805 although 160 entries were free in all. A store NVS still refuses is a skip, not a failure:
    the setup, not the firmware under test, is what could not run."""
    w = usb_wcb(bench)
    m = "Q" + nonce()[:1]
    problems = []
    with config_guard(bench, 1):
        try:
            out = w.run("?SEQ,SAVE,HILBIG," + "^".join([f";S0{m}"] * 210), timeout=8)
            if _nvs_refused(out):
                raise Skip("W1's store has no room for the 1.26 KB setup sequence (see ?NVS); the queue refusal was not "
                           "exercised")
            if not _has(out, "Stored: Key='HILBIG'"):
                raise AssertionError(f"setup: the 210-command sequence was not stored: {out[:3]}")
            wm = w.dev.mark()
            w.dev.send(";CHILBIG,L")
            w.dev.expect(r"^Sequence 'HILBIG' has 210 commands and the command queue has room for \d+ .* not run\.",
                         timeout=5, since=wm)
            time.sleep(3.0)
            lines = [x.rstrip() for x in w.dev.since(wm)]
            ran = sum(1 for x in lines if x == m)
            full = sum(1 for x in lines if x.startswith("Command queue is full"))
            if ran or full:
                problems.append(f"{ran} of its commands ran and {full} 'queue is full' line(s) printed")
        finally:
            w.run("?SEQ,CLEAR,HILBIG")
    assert not problems, "; ".join(problems)


@test("seq.recall_forms", ";C / ;c and ;SEQ in any case recall with ,L / ,LOCAL / ,l; a wrong-case key and the empty-key forms are refused (;Seq since tracker #95)", needs=["wcb1"])
def recall_forms(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    a = marker("a")
    c_usage = "Invalid recall command. Use ;Ckey (or ;Ckey,L for local-only)."
    cases = [(";SEQHILR1,L", True, ["Recall stored command request received:SEQHILR1,L", "Recalling stored sequence command...",
                                     f"Recalling command for key 'HILR1': ;S1{a}"]),
             (";seqHILR1,L", True, []), (";cHILR1,LOCAL", True, []), (";CHILR1,l", True, []),
             (";SeqHILR1,L", True, ["Recalling stored sequence command..."]),
             (";sEQHILR1,L", True, ["Recalling stored sequence command..."]),
             (";C", False, [c_usage]), (";SEQ", False, ["Invalid recall command. Use SEQkey (or SEQkey,L for local-only)."]),
             (";C,L", False, [c_usage]), (";CHILNOPE,L", False, ["No command stored under key: 'HILNOPE'"]),
             (";Chilr1,L", False, ["No command stored under key: 'hilr1'"])]
    bad = []
    with config_guard(bench, 1):
        try:
            w.run(f"?SEQ,SAVE,HILR1,;S1{a}")
            for cmd, arrives, wants in cases:
                pm = s1.mark()
                out = w.run(cmd)
                time.sleep(0.3)
                if (a.encode() + b"\r" in s1.received(pm)) != arrives:
                    bad.append(f"{cmd}: marker {'missing' if arrives else 'arrived'}")
                bad += [f"{cmd}: no {x!r}" for x in wants if not _has(out, x)]
                if cmd.startswith((";c", ";C")) and arrives and _has(out, "Recalling stored sequence command..."):
                    bad.append(f"{cmd}: the ;C form printed the ;SEQ line")
        finally:
            _clear_seq(w, "HILR1")
    assert not bad, "; ".join(bad)


@test("seq.comments_stripped", "Recall strips *** comments from each part; a comment-only sequence says so", needs=["wcb1"])
def comments_stripped(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    a, b = marker("a"), marker("b")
    body = f";S1{a}***note^***whole^;S1{b}"
    with config_guard(bench, 1):
        try:
            w.run(f"?SEQ,SAVE,HILCM,{body}")
            w.run("?SEQ,SAVE,HILCO,***only a note")
            pm = s1.mark()
            out = w.run(";CHILCM,L")
            time.sleep(0.5)
            got = s1.received(pm)
            only = w.run(";CHILCO,L")
        finally:
            _clear_seq(w, "HILCM", "HILCO")
    assert _has(out, f"Recalling command for key 'HILCM': {body}"), out
    assert got == f"{a}\r{b}\r".encode(), f"W1 S1 got {got!r}"
    assert _has(only, "Sequence contained only comments — nothing to execute."), only


@test("seq.save_boundary", "A ?SEQ,SAVE value ends only at '^?'; '^;' and '^text' stay; a trailing '?' is kept, whatever the case of the verb ('?Seq,' since tracker #95)", needs=["wcb1"])
def save_boundary(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    a, b, c, d = marker("a"), marker("b"), marker("c"), marker("d")
    bad = []
    with config_guard(bench, 1):
        try:
            out = w.run(f"?SEQ,SAVE,HILB1,;S1{a}^?VERSION")
            if not _has(out, f"Stored: Key='HILB1', Value=';S1{a}'") or not _has(out, "Software Version: "):
                bad.append(f"'^?' did not end the value and run ?VERSION: {out}")
            if not _has(w.run(f"?SEQ,SAVE,HILB2,;S1{a}^;S1{b}^plain"), f"Stored: Key='HILB2', Value=';S1{a}^;S1{b}^plain'"):
                bad.append("'^;' or '^text' ended the value")
            if _seqval(w, "HILB1") != f"[MGMT:SEQVAL,1]HILB1,OK,;S1{a}" or _seqval(w, "HILB2") != f"[MGMT:SEQVAL,1]HILB2,OK,;S1{a}^;S1{b}^plain":
                bad.append("GET values differ")
            if not _has(w.run(f"?SEQ,SAVE,HILQ,;S1{c}?"), f"Stored: Key='HILQ', Value=';S1{c}?'"):
                bad.append("a trailing '?' on ?SEQ,SAVE triggered the help trap")
            pm = s1.mark()
            w.send(";CHILQ,L")
            try:
                s1.expect(f"{c}?\r".encode(), timeout=2, since=pm)
            except AssertionError:
                bad.append("the recalled value lost its '?'")
            # The dispatcher takes a verb in any case, so the trailing-'?' exemption does too (tracker #95): it used to
            # print the help page here and store nothing.
            out = w.run(f"?Seq,SAVE,HILQ2,;S1{d}?")
            if not _has(out, f"Stored: Key='HILQ2', Value=';S1{d}?'") or _has(out, "Command Reference"):
                bad.append(f"'?Seq,SAVE' with a trailing '?' was not stored as typed: {out[:3]}")
            if _seqval(w, "HILQ2") != f"[MGMT:SEQVAL,1]HILQ2,OK,;S1{d}?":
                bad.append("'?Seq,SAVE' stored a different value")
        finally:
            _clear_seq(w, "HILB1", "HILB2", "HILQ", "HILQ2")
    assert not bad, "; ".join(bad)


def _live_funcchar(w):
    """The live function identifier: the first character of the configured chain WCB_WEBTOOL_CONFIG_PULL prints - a
    line W1 recognises whatever the identifier is (s18's _live_chars reads it the same way)."""
    m = w.dev.mark()
    w.dev.send("WCB_WEBTOOL_CONFIG_PULL")
    w.dev.expect(r"For Configured Boards", timeout=5, since=m)
    time.sleep(1.5)
    lines = w.dev.since(m)
    at = next(k for k, x in enumerate(lines) if "For Configured Boards" in x)
    chain = next((x.strip() for x in lines[at + 1:] if re.search(r"CHK[0-9A-Fa-f]{8}\s*$", x)), "")
    return chain[:1] or "?"


def _pull_spaced(dev, wcb):
    """Wait out PULL_SPACING_S from the harness's last pull of board wcb (hil.wcb.Pull stamps dev._last_pull): a target
    answers one pull per requester per 1.5 s and drops the next as a duplicate."""
    last = dev.__dict__.setdefault("_last_pull", {}).get(wcb)
    if last is not None and PULL_SPACING_S - (time.monotonic() - last) > 0:
        time.sleep(PULL_SPACING_S - (time.monotonic() - last))


def _await_pull(dev, wcb, since, timeout=15.0):
    """Read the reply to a hand-typed ?MGMT,PULL,<wcb> sent at `since` until it is whole or refused, then stamp it as
    hil.wcb.Pull does, so the harness spaces its next pull of that board from it. Returns 'complete', the refusal's
    code, or 'timeout' - never the reply itself, which carries the mesh password."""
    col, i, result = PullCollector(wcb, since), since, "timeout"
    deadline = time.monotonic() + timeout
    while result == "timeout" and time.monotonic() < deadline:
        for line in dev.since(i):
            i += 1
            try:
                if col.feed(line) == "complete":
                    result = "complete"
                    break
            except PullRefused as e:
                result = e.code
                break
        if result == "timeout":
            time.sleep(0.1)
    dev.__dict__.setdefault("_last_pull", {})[wcb] = time.monotonic()
    return result


@test("seq.chain_edge_forms", "Chain-walker edge forms: with a letter as the function identifier ('x') 'xseq,save,' keeps its value whole, as does a lowercase '?seq,save,'; an IF-first line whose ?CS value holds ;T stores it whole and runs none of it; a ?MGMT, token that is not first takes the rest of its line (the ;S0 before it runs, ?MGMT,SEQ,2 is answered, the ?VERSION after it does not run); '?MGMT,PULL,2, p' is a parts request", needs=["wcb1"])
def chain_edge_forms(bench):
    """WCB-WP55 row 3. tokenHasVerb (WCB.ino:2616-2624) matches the function identifier exactly and the verb in any
    case, so parseCommandsNoChecksum's whole-token branches - a ?CS or ?SEQ,SAVE value ends only at delimiter +
    identifier, and ?MGMT, takes the rest of the line by design (:2797-2870) - hold for a letter identifier and a
    lowercase verb. chainCarriesValueVerb (:2632-2645) keeps a line carrying one of those verbs off the timer splitter
    wherever the verb sits (isTimerChain :2678-2682, the reader :8570-8587): isTimerCommand alone matches ';T' anywhere
    in a line (command_timer.cpp:71-74). handleMgmtPullRequest trims and upper-cases the option, so ', p' asks for parts
    (WCB.ino:5032-5036); both ends say so under ?DEBUG,MGMT (:5058-5060 on the relay, :3999-4000 on the target). A
    relay's parts request can be lost whole while its plain copy lands (hil.wcb Pull), so the pull is sent a second time
    before W2's missing '(parts accepted)' counts. While the identifier is 'x' every '?' line is plain text, so that
    window sends none (chars.funcchar_change_restore). Every value's markers go to W1 S1, which must stay silent."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    mk = {k: marker(k) for k in "abcdef"}
    v1, v2 = f";S1{mk['a']}^;S1{mk['b']}", f";S1{mk['c']}^;S1{mk['d']}"
    v4 = f";S1{mk['e']}^;T300^;S1{mk['f']}"
    problems, notes = [], []
    with config_guard(bench, 1) as before:
        if token(before[1], "?CMDCHAR,") != "?CMDCHAR,;":
            raise Skip("W1's command character is not ';'")
        _clear_seq(w, "HILLC", "HILLC2", "HILLT")
        pm = s1.mark()
        try:
            # (1) a letter as the function identifier
            changed = False
            try:
                out = w.run("?FUNCCHAR,x")
                changed = _has(out, "Local function identifier updated to 'x'")
                if not changed:
                    problems.append(f"?FUNCCHAR,x printed {out}")
                else:
                    out = w.run(f"xseq,save,HILLC,{v1}")
                    if _nvs_refused(out):
                        raise Skip("W1's NVS refused a 30-character sequence (see ?NVS)")
                    if not _has(out, f"Stored: Key='HILLC', Value='{v1}'"):
                        problems.append(f"'xseq,save,' under the identifier 'x' printed {out}")
                    got = next((x.rstrip() for x in w.run("xSEQ,GET,HILLC") if x.startswith("[MGMT:SEQVAL,")), None)
                    if got != f"[MGMT:SEQVAL,1]HILLC,OK,{v1}":
                        problems.append(f"xSEQ,GET,HILLC read {got!r}")
            finally:
                if changed and not _has(w.run("xFUNCCHAR,?"), "Local function identifier updated to '?'"):
                    lfi = _live_funcchar(w)
                    if lfi != "?":
                        w.dev.send(f"{lfi}FUNCCHAR,?")
                        time.sleep(0.5)
            # (2) a lowercase verb
            out = w.run(f"?seq,save,HILLC2,{v2}")
            if _nvs_refused(out):
                raise Skip("W1's NVS refused a 30-character sequence (see ?NVS)")
            if not _has(out, f"Stored: Key='HILLC2', Value='{v2}'") or _seqval(w, "HILLC2") != f"[MGMT:SEQVAL,1]HILLC2,OK,{v2}":
                problems.append(f"'?seq,save,' stored {_seqval(w, 'HILLC2')!r}: {out}")
            # (4) an IF-first line whose ?CS value holds ;T (the IF reads an unset variable as 0, so it lets ?CS run)
            out = w.run(f"IF,hilq{nonce().lower()[:4]}=0^?CSHILLT,{v4}")
            if _nvs_refused(out):
                raise Skip("W1's NVS refused a 40-character sequence (see ?NVS)")
            if not _has(out, f"Stored: Key='HILLT', Value='{v4}'") or _seqval(w, "HILLT") != f"[MGMT:SEQVAL,1]HILLT,OK,{v4}":
                problems.append(f"the IF-first ?CS stored {_seqval(w, 'HILLT')!r}: {out}")
            time.sleep(1.0)                        # the ;T300 in the value, had it run
            ran = [k for k, t in mk.items() if t.encode() in s1.received(pm)]
            if ran:
                problems.append(f"markers from the stored values reached W1 S1: {ran}")
            if 2 in bench.wcb_numbers():
                # (3) a ?MGMT, token that is not first
                time.sleep(1.7)                    # W2 answers one SEQ_REQ per requester per 1.5 s
                t = marker("m")
                wm = w.dev.mark()
                out = [x.rstrip() for x in w.run(f";S0{t}^?MGMT,SEQ,2^?VERSION")]
                if t not in out:
                    problems.append("the ;S0 token ahead of ?MGMT, did not run")
                if _has(out, "Software Version:"):
                    problems.append("the ?VERSION inside the ?MGMT, token ran as a command of its own")
                try:
                    w.dev.expect(r"^\[MGMT:SEQ,2\]", timeout=6, since=wm)
                except AssertionError:
                    problems.append("?MGMT,SEQ,2 behind a ;S0 token brought no [MGMT:SEQ,2] reply")
                # (5) ', p' is a parts request
                with Console(bench, 2) as c2:
                    c2.expect(r"MGMT debugging enabled", timeout=3, since=c2.send("?DEBUG,MGMT,ON"))
                    w.run("?DEBUG,MGMT,ON")
                    try:
                        relay_ok, target, reply = False, None, None
                        for _ in range(2):
                            _pull_spaced(w.dev, 2)
                            wm, cm = w.dev.mark(), c2.mark()
                            w.dev.send("?MGMT,PULL,2, p")
                            try:
                                w.dev.expect(r"^\[MGMT\] Config pull request sent for WCB2 \(x3, parts accepted\)",
                                             timeout=4, since=wm)
                                relay_ok = True
                            except AssertionError:
                                relay_ok = False
                            try:
                                target = c2.expect(r"\[MGMT\] Config request from WCB1( \(parts accepted\))?$",
                                                   timeout=6, since=cm).group(1)
                            except AssertionError:
                                target = None
                            reply = _await_pull(w.dev, 2, wm)
                            if not relay_ok or target:
                                break
                        notes.append(f"', p' pull answered: {reply}")
                        if not relay_ok:
                            problems.append("W1 did not take '?MGMT,PULL,2, p' as a parts request: no '(x3, parts "
                                            "accepted)' line under ?DEBUG,MGMT")
                        elif not target:
                            problems.append("W2 never logged the ', p' pull with '(parts accepted)' (twice)")
                    finally:
                        w.run("?DEBUG,MGMT,OFF")
                        c2.send("?DEBUG,MGMT,OFF")
                        time.sleep(0.3)
            else:
                notes.append("no WCB2: the ?MGMT forms were not run")
        finally:
            _clear_seq(w, "HILLC", "HILLC2", "HILLT")
    if notes:
        bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


REFUSE ="Sequence '{}' recalls itself (directly or in a loop) — refusing to expand it again. Break the cycle in the stored sequence."


@test("seq.cycle_guard", "Self-recursion (direct and ring) is refused after one expansion; nesting stops at 8; the queue keeps working", needs=["wcb1"])
def cycle_guard(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    mk = {k: marker(k) for k in "abcd"}
    depth = [marker(f"m{i}") for i in range(1, 10)]
    saves = [("HILRR", f";S1{mk['a']}^;CHILRR"), ("HILRS", f";S1{mk['b']}^;SEQHILRS"),
             ("HILRA", f";S1{mk['c']}^;CHILRB"), ("HILRB", f";S1{mk['d']}^;CHILRA")]
    saves += [(f"HILD{k}", f";S1{depth[k - 1]}^;CHILD{k + 1}") for k in range(1, 9)] + [("HILD9", f";S1{depth[8]}")]
    with config_guard(bench, 1):
        try:
            for k, v in saves:
                w.run(f"?SEQ,SAVE,{k},{v}")
            pm, wm = s1.mark(), w.dev.mark()
            for cmd, wait in ((";CHILRR,L", 2), (";SEQHILRS,L", 2), (";CHILRA,L", 2), (";CHILD1,L", 3)):
                w.send(cmd)
                time.sleep(wait)
            try:
                w.version()      # a regressed guard would refill the queue forever
            except AssertionError:
                w.reboot()
                raise
            got, lines = s1.received(pm), [x.rstrip() for x in w.dev.since(wm)]
        finally:
            _clear_seq(w, *(k for k, _ in saves))
    want = b"".join(x.encode() + b"\r" for x in [mk["a"], mk["b"], mk["c"], mk["d"]] + depth[:8])
    assert got == want, f"W1 S1 got {got!r}, expected {want!r}"
    missing = [k for k in ("HILRR", "HILRS", "HILRA") if REFUSE.format(k) not in lines]
    assert not missing, f"no refusal line for {missing}"
    assert "Sequence nesting deeper than 8 — refusing to expand 'HILD9'." in lines, "no depth-limit line"


@test("seq.cycle_guard_case", "(should) A body recalling a different-case key that does not exist reports it missing, not a cycle", needs=["wcb1"])
def cycle_guard_case(bench):
    """Tracker #40: the guard compares the exact key bytes (seqKeyHash, recallStoredCommand in WCB.ino), like the
    case-sensitive NVS lookup, so a wrong-case key that does not exist reports missing instead of a cycle."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    e = marker("e")
    with config_guard(bench, 1):
        try:
            w.run(f"?SEQ,SAVE,HILRX,;S1{e}^;Chilrx")
            pm, wm = s1.mark(), w.dev.mark()
            w.send(";CHILRX,L")
            time.sleep(2)
            got, lines = s1.received(pm), [x.rstrip() for x in w.dev.since(wm)]
        finally:
            _clear_seq(w, "HILRX")
    assert got == e.encode() + b"\r", f"W1 S1 got {got!r}"
    assert "No command stored under key: 'hilrx'" in lines and REFUSE.format("hilrx") not in lines, \
        f"lines {[x for x in lines if 'hilrx' in x]}"


@test("seq.cycle_guard_reuse", "(should) A sub-sequence reused in one run expands every time: back-to-back, across a ;t delay, at two depths, nine calls under one trigger; a ;t-delayed self-recall is still refused", needs=["wcb1"])
def cycle_guard_reuse(bench):
    """Tracker #47: the guard's key set was never popped, so a second call to the same sub-sequence was refused as
    recursion, and a flat body calling 8 different ones hit 'nesting deeper than 8'. S1 order is FIFO: a body's own
    tokens drain before the bodies they recall (recallCommandSlot enqueues, it does not recurse)."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    leaf = [marker(f"l{i}") for i in range(1, 9)]
    r = marker("r")
    saves = [(f"HILL{i}", f";S1{leaf[i - 1]}") for i in range(1, 9)]
    saves += [("HILUW", "^".join(f";CHILL{i}" for i in range(1, 9)) + "^;CHILL1"),  # 8 distinct, then a repeat
              ("HILUT", ";CHILL2^;t300^;CHILL2"),                                       # reuse across a ;t delay
              ("HILUM", ";CHILL3"), ("HILUN", ";CHILUM^;CHILL3"),                        # one leaf at depth 2 and 1
              ("HILUR", f";S1{r}^;t200^;CHILUR")]                                        # paced self-recall: a cycle
    with config_guard(bench, 1):
        try:
            for k, v in saves:
                w.run(f"?SEQ,SAVE,{k},{v}")
            pm, wm = s1.mark(), w.dev.mark()
            for cmd in (";CHILUW,L", ";CHILUT,L", ";CHILUN,L", ";CHILUR,L"):
                w.send(cmd)
                time.sleep(2)
            try:
                w.version()      # a guard that lost the lineage would refill the queue forever
            except AssertionError:
                w.reboot()
                raise
            got, lines = s1.received(pm), [x.rstrip() for x in w.dev.since(wm)]
        finally:
            w.send("?STOP")      # a lineage lost across ;t would leave HILUR re-arming its own timer chain
            _clear_seq(w, *(k for k, _ in saves))
    want = b"".join(x.encode() + b"\r" for x in leaf + [leaf[0], leaf[1], leaf[1], leaf[2], leaf[2], r])
    assert got == want, f"W1 S1 got {got!r}, expected {want!r}"
    refused = [x for x in lines if "recalls itself" in x or "nesting deeper" in x]
    assert refused == [REFUSE.format("HILUR")], f"refusal lines {refused}"
    assert not any("No command stored under key" in x for x in lines), "a recall missed its key"
    assert not any("queue is full" in x or "nearly full" in x for x in lines), "a small reuse ran into the queue limit"


@test("seq.reuse_queue_reserve", "(should) Back-to-back reuse that would overflow the command queue refuses whole nested expansions with one line each, never cuts a body short, and the board keeps answering", needs=["wcb1"])
def reuse_queue_reserve(bench):
    """Tracker #47 follow-on: once reuse runs, 12 back-to-back calls of a 20-command sub-sequence expand every call
    before any leaf drains (FIFO), ~240 tokens against the 200-slot queue. recallCommandSlot refuses a NESTED
    expansion that would leave fewer than SEQ_QUEUE_RESERVE (16) slots free, so the tail calls print one 'nearly
    full' line each instead of one 'queue is full' line per dropped token (UART0 has no TX buffer). The model
    predicts 9 expanded + 3 refused; the test asserts only that every call is accounted for, whole."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    q = marker("q")
    calls = 12
    with config_guard(bench, 1):
        try:
            w.run("?SEQ,SAVE,HILQL," + "^".join([f";S1{q}"] * 20))
            w.run("?SEQ,SAVE,HILQW," + "^".join([";CHILQL"] * calls))
            pm, wm = s1.mark(), w.dev.mark()
            w.send(";CHILQW,L")
            time.sleep(6)
            try:
                w.version()      # the board must still answer: no flood of queue-full lines on UART0
            except AssertionError:
                w.reboot()
                raise
            got, lines = s1.received(pm), [x.rstrip() for x in w.dev.since(wm)]
        finally:
            _clear_seq(w, "HILQL", "HILQW")
    block = (q.encode() + b"\r") * 20
    whole, rest = divmod(len(got), len(block))
    assert not rest and got == block * whole, f"W1 S1 got a partial body ({len(got)} bytes, block {len(block)})"
    nearly = [x for x in lines if x.startswith("Command queue nearly full") and "'HILQL'" in x]
    assert nearly, "no 'nearly full' refusal: the queue reserve did not engage"
    assert whole + len(nearly) == calls, f"{whole} expanded + {len(nearly)} refused != {calls} calls"
    assert not any("Command queue is full" in x for x in lines), "tokens were dropped one by one"
    assert not any("recalls itself" in x or "nesting deeper" in x for x in lines), "reuse was refused as a cycle"


@test("seq.local_suffix_and_seq_fanout", ",L keeps a recall off the mesh; top-level ;C<key> and ;SEQ<key> both run W2's copy", needs=["wcb1"])
def local_suffix_and_seq_fanout(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    t2 = marker()
    bad = []
    with config_guard(bench, 2):
        w.send(f";W2,?SEQ,SAVE,HILML,;S2{t2}")
        time.sleep(1.5)
        try:
            m = s2.mark()
            out = w.run(";CHILML,L")
            time.sleep(3)
            if s2.received(m) or not _has(out, "No command stored under key: 'HILML'"):
                bad.append(f";CHILML,L left W1: W2 S2 {s2.received(m)!r}, W1 said {out}")
            for cmd in (";CHILML", ";SEQHILML"):
                m = s2.mark()
                w.send(cmd)
                try:
                    s2.expect(t2.encode() + b"\r", timeout=3, since=m)
                except AssertionError:
                    bad.append(f"{cmd} did not run W2's copy")
        finally:
            w.send(";W2,?SEQ,CLEAR,HILML")
            time.sleep(1.5)
    assert not bad, "; ".join(bad)


@test("seq.nested_no_fanout", "A ;C inside a sequence body runs locally only, even when the top-level recall fans out", needs=["wcb1"])
def nested_no_fanout(bench):
    s1, s2 = link(bench, 1, "S1"), link(bench, 2, "S2")
    w = usb_wcb(bench)
    t1, t2 = marker("a"), marker("b")
    with config_guard(bench, 1, 2):
        try:
            w.run(f"?SEQ,SAVE,HILNS,;S1{t1}")
            w.run("?SEQ,SAVE,HILNO,;CHILNS")
            w.send(f";W2,?SEQ,SAVE,HILNS,;S2{t2}")
            time.sleep(1.5)
            m1, m2 = s1.mark(), s2.mark()
            w.send(";CHILNO")
            time.sleep(3)
            got1, got2 = s1.received(m1), s2.received(m2)
        finally:
            _clear_seq(w, "HILNS", "HILNO")
            w.send(";W2,?SEQ,CLEAR,HILNS")
            time.sleep(1.5)
    assert t1.encode() + b"\r" in got1, f"the nested recall did not run on W1: {got1!r}"
    assert t2.encode() not in got2, "the nested ;CHILNS fanned out to W2"


# ============================================================ legacy ?CS / ?CE
@test("legacy.cs_ce", "Legacy ?CS<key>,<value> saves (keeping ^ chains), two ?CS on one line split at '^?CS', ?CE erases, either case", needs=["wcb1"])
def cs_ce(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    a, b, c, d = marker("a"), marker("b"), marker("c"), marker("d")
    bad = []
    with config_guard(bench, 1):
        try:
            if not _has(w.run(f"?CSHILLG,;S1{a}^;S1{b}"), f"Stored: Key='HILLG', Value=';S1{a}^;S1{b}'"):
                bad.append("?CS did not keep the ^ chain")
            pm = s1.mark()
            w.send(";CHILLG,L")
            try:
                s1.expect(f"{a}\r{b}\r".encode(), timeout=2, since=pm)
            except AssertionError:
                bad.append("the ?CS sequence did not recall")
            out = w.run(f"?csHILLG2,;S1{c}^?CSHILLH,;S1{d}")
            if not (_has(out, f"Stored: Key='HILLG2', Value=';S1{c}'") and _has(out, f"Stored: Key='HILLH', Value=';S1{d}'")):
                bad.append(f"two ?CS on one line: {out}")
            for cmd, key in (("?CEHILLG", "HILLG"), ("?ceHILLG2", "HILLG2"), ("?CEHILLH", "HILLH")):
                if not _has(w.run(cmd), f"Deleted stored command key: '{key}'"):
                    bad.append(f"{cmd} did not erase")
            if _seqval(w, "HILLG") != "[MGMT:SEQVAL,1]HILLG,NOTFOUND,":
                bad.append("HILLG still reads back")
        finally:
            _clear_seq(w, "HILLG", "HILLG2", "HILLH")
    assert not bad, "; ".join(bad)


@test("legacy.cs_caret_lfi_roundtrip", "(should) Legacy ?CS never stores a value containing '^?', which ?backup cannot restore", needs=["wcb1"], links=[])
def cs_caret_lfi_roundtrip(bench):
    """Probable bug: ?CS ends a value only at '^?CS' (WCB.ino:2295-2301) but backup restore ends ?SEQ,SAVE at '^?'
    (WCB.ino:2330-2335), so ?CSkey,a^?VERSION is stored whole and backed up in a form that restores as 'a' plus a
    ?VERSION. The key is cleared before config_guard's after-snapshot: while stored it breaks group_tokens."""
    w = usb_wcb(bench)
    a = marker("a")
    with config_guard(bench, 1):
        try:
            out = w.run(f"?CSHILLQ,;S1{a}^?VERSION")
            val = _seqval(w, "HILLQ")
            backed = _has(w.run("?backup", timeout=8), f"?SEQ,SAVE,HILLQ,;S1{a}^?VERSION")
        finally:
            _clear_seq(w, "HILLQ")
    bench.note(f"?CS with '^?': ran ?VERSION: {_has(out, 'Software Version:')}; stored {val!r}; in ?backup: {backed}")
    assert val != f"[MGMT:SEQVAL,1]HILLQ,OK,;S1{a}^?VERSION", "?CS stored a value containing '^?'"


@test("legacy.cs_checksum", "?CS with a trailing ^?CHK: a good CRC saves, a bad one refuses, lowercase and short CRCs are accepted", needs=["wcb1"], links=[])
def cs_checksum(bench):
    w = usb_wcb(bench)
    a, b, c = marker("a"), marker("b"), marker("c")
    bad = []
    with config_guard(bench, 1):
        try:
            l1 = f"?CSHILCK,;S1{a}^;S1{b}"
            out = w.run(f"{l1}^?CHK{chain_crc(l1)}")
            if not (_has(out, "Command checksum VERIFIED") and _has(out, f"Stored: Key='HILCK', Value=';S1{a}^;S1{b}'")):
                bad.append(f"good CRC: {out}")
            l2 = f"?CSHILCK2,;S1{a}"
            out = [x.rstrip() for x in w.run(f"{l2}^?CHK00000000")]
            if not (_has(out, "Command checksum FAILED!") and "  Provided:   00000000" in out and f"  Calculated: {chain_crc(l2)}" in out):
                bad.append(f"bad CRC: {out}")
            if _seqval(w, "HILCK2") != "[MGMT:SEQVAL,1]HILCK2,NOTFOUND,":
                bad.append("the bad-CRC line was stored")
            l3 = f"?CSHILCK3,;S1{c}"
            out = w.run(f"{l3}^?chk{chain_crc(l3).lower()}")
            if not (_has(out, "Command checksum VERIFIED") and _has(out, "Stored: Key='HILCK3'")):
                bad.append(f"lowercase CRC: {out}")
            l4 = next(x for x in (f"?CSHILCK4,;S1{marker('d')}" for _ in range(400)) if chain_crc(x).startswith("0"))
            out = w.run(f"{l4}^?CHK{chain_crc(l4).lstrip('0') or '0'}")
            if not _has(out, "Command checksum VERIFIED"):
                bad.append(f"short CRC (leading zeros dropped): {out}")
        finally:
            _clear_seq(w, "HILCK", "HILCK2", "HILCK3", "HILCK4")
    assert not bad, "; ".join(bad)


# ============================================================ inventory relay (?MGMT,SEQ / ?MGMT,SEQGET)
_LAST_REQ = {}
RELAY_SPACING_S = 2.0   # the target ignores a repeat for 1500 ms after it ANSWERED, not after we asked


def _relay(w, cmd, pattern, space_key, timeout=3.0, tries=3):
    """Send a relay request at least RELAY_SPACING_S after the last one with the same dedup key; return every matching
    W1 line of the answered try (first match plus 0.5 s). The answer comes back as unacknowledged broadcast frags, in
    two passes since 2026-10-06 (sendResultFrags, HIL_FIX_TRACKER #109; once before), so only a frag lost from both
    passes means no line and no error, and the requester is expected to retry (docs/SEQUENCE_INVENTORY.md §3a;
    inv.mgmt_seq_remote missed one on 2026-09-23, tracker #77)."""
    for attempt in range(1, tries + 1):
        last = _LAST_REQ.get(space_key)
        if last is not None:
            wait = RELAY_SPACING_S - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        m = w.dev.mark()
        w.send(cmd)
        _LAST_REQ[space_key] = time.monotonic()
        try:
            w.dev.expect(pattern, timeout=timeout, since=m)
        except AssertionError:
            if attempt == tries:
                raise
            continue
        time.sleep(0.5)
        return [x.rstrip() for x in w.dev.since(m) if re.search(pattern, x)]


def _inventory_of(w, wcb):
    lines = _relay(w, f"?MGMT,SEQ,{wcb}", rf"^\[MGMT:SEQ,{wcb}\]", f"SEQ{wcb}")
    if len(lines) != 1:
        raise AssertionError(f"?MGMT,SEQ,{wcb} answered {len(lines)} times: {lines}")
    return _parse_names(lines[0])


@test("inv.mgmt_seq_remote", "?MGMT,SEQ,2 returns one [MGMT:SEQ,2] line that tracks a remote save and clear", needs=["wcb1"], links=[])
def mgmt_seq_remote(bench):
    w = usb_wcb(bench)
    a = marker("a")
    bad = []
    with config_guard(bench, 2):
        h0, n0, _ = _inventory_of(w, 2)
        try:
            w.send(f";W2,?SEQ,SAVE,HILI1,{a}")
            time.sleep(1.5)
            h1, n1, names1 = _inventory_of(w, 2)
            if n1 != n0 + 1 or names1[-1:] != ["HILI1"] or h1 == h0:
                bad.append(f"after the save: {h1} {n1} {names1[-1:]}")
            w.send(";W2,?SEQ,CLEAR,HILI1")
            time.sleep(1.5)
            h2, n2, _ = _inventory_of(w, 2)
            if (h2, n2) != (h0, n0):
                bad.append(f"after the clear: {h2} {n2}, expected {h0} {n0}")
        finally:
            w.send(";W2,?SEQ,CLEAR,HILI1")
            time.sleep(1.0)
    assert not bad, "; ".join(bad)


@test("inv.hash_matches_wdp", "W2's SEQHASH advert updates within seconds and equals ?MGMT,SEQ; W1's self record equals ?SEQ,NAMES", needs=["wcb1"], links=[])
def hash_matches_wdp(bench):
    w = usb_wcb(bench)
    a = marker("a")
    with config_guard(bench, 2):
        try:
            w.send(f";W2,?SEQ,SAVE,HILW1,{a}")
            time.sleep(1.5)
            h2 = _inventory_of(w, 2)[0]
            deadline, seen = time.monotonic() + 6, False
            while not seen and time.monotonic() < deadline:
                seen = _has(w.run("?WDP,DUMP", timeout=8), f"[WDPSEQ:N=2,HASH={h2}]")
                if not seen:
                    time.sleep(1)
            detail = w.run("?WDP,DETAIL,2", timeout=5)
            h1 = _names(w)[0]
            dump = w.run("?WDP,DUMP", timeout=8)
        finally:
            w.send(";W2,?SEQ,CLEAR,HILW1")
            time.sleep(1.0)
    assert seen, f"W1's DUMP never showed [WDPSEQ:N=2,HASH={h2}]"
    assert _has(detail, f"  Sequences   : hash {h2}   (?MGMT,SEQ,2 to list)"), "DETAIL,2 lacks the Sequences line"
    assert _has(dump, f"[WDPSEQ:N=1,HASH={h1}]"), f"W1's self record is not {h1}"


@test("inv.client_no_hash", "A WCB_Client neighbour (NaviCore 20) advertises no SEQHASH: no WDPSEQ record, no Sequences line in DETAIL", needs=["wcb1"], links=[])
def client_no_hash(bench):
    w = usb_wcb(bench)
    dump = w.run("?WDP,DUMP", timeout=8)
    if not any(x.startswith("[WDP:N=20,CLIENT=1,") for x in dump):
        raise Skip("NaviCore 20 is not in W1's WDP table")
    detail = [x.rstrip() for x in w.run("?WDP,DETAIL,20", timeout=5)]
    assert not any(x.startswith("[WDPSEQ:N=20,") for x in dump), "NaviCore has a WDPSEQ record"
    assert any(x.startswith("==== Device 20") for x in detail), f"DETAIL,20 header missing: {detail[:3]}"
    assert not any(x.startswith("  Sequences") for x in detail), "the client DETAIL block has a Sequences line"


VALUE_ASKED = re.compile(r"^\[MGMT\] Sequence-value request '([^']*)' from WCB(\d+) \(")   # WCB.ino handleSeqValReqPacket
NAMES_ASKED = re.compile(r"^\[MGMT\] Sequence-names request from WCB(\d+)$")              # WCB.ino handleSeqReqPacket


def dedup_verdict(answered, printed, want):
    """inv.dedup's judgment of one window -> (problem or None, how many replies were lost on the air). `answered`: the
    keys W2 says it answered, in order (its ?DEBUG,MGMT lines), or None when W2 has no USB cable here; `printed`: the
    keys W1 printed replies for; `want`: the keys the dedup rule answers. With W2's lines, the rule is judged on them
    alone, and a key W2 answered that W1 never printed is a reply lost on the air (unacknowledged broadcast frames,
    sendResultFrags), counted, not failed; W1 printing a reply W2 did not send fails. Without them,
    W1's replies are the only witness and must equal `want`."""
    if answered is None:
        return (None if sorted(printed) == sorted(want) else f"W1 printed {printed}, the rule answers {want}"), 0
    if answered != want:
        return f"W2 answered {answered}, the rule answers {want}", 0
    left = list(answered)
    for k in printed:
        if k not in left:
            return f"W1 printed {printed}, more than W2 answered ({answered})", 0
        left.remove(k)
    return None, len(left)


@test("inv.dedup", "Relay dedup: one SEQ_REQ answer per requester per 1.5 s; SEQVAL dedup remembers only the last (requester, key) — timing-sensitive", needs=["wcb1"], links=[])
def dedup(bench):
    """WCB.ino handleSeqReqPacket and handleSeqValReqPacket: W1 broadcasts each request 3 times, and W2 answers a
    requester's SEQ once per 1.5 s and a SEQGET once per 1.5 s per (requester, key) - only the last key is kept, so
    HILSA, HILSB, HILSA 150 ms apart is answered three times and a fourth HILSA 500 ms later is dropped. When W2 has
    its own USB cable, its ?DEBUG,MGMT lines (RAM only) say what it answered: the rule is judged on those, and a reply
    W2 sent that W1 never printed is lost on the air - noted, and one such loss allowed per run. Run 20261006-122850
    lost the +300 ms HILSA reply that way, then sent once as one unacknowledged broadcast frame; replies have gone in
    two passes since (sendResultFrags, HIL_FIX_TRACKER #109). Without W2's lines that read as a dedup failure."""
    w = usb_wcb(bench)
    w2 = WCB(bench.dev("wcb2")) if bench.has("wcb2") else None
    me = str(bench.usb_wcb_number())
    a, b = marker("a"), marker("b")
    sa, sb = f"[MGMT:SEQVAL,2]HILSA,OK,{a}", f"[MGMT:SEQVAL,2]HILSB,OK,{b}"

    def lines_since(m, prefix):
        return [x.rstrip() for x in w.dev.since(m) if x.startswith(prefix)]

    def asked(m2, rx):
        """What W2 says it answered since mark m2 (requests from this W1 only), or None without W2's USB."""
        if w2 is None:
            return None
        out = []
        for x in w2.dev.since(m2):
            g = rx.match(x.strip())
            if g and g.groups()[-1] == me:
                out.append(g.group(1) if g.re is VALUE_ASKED else "SEQ")
        return out

    def mark2():
        return w2.dev.mark() if w2 is not None else None

    with config_guard(bench, 2):
        try:
            if w2 is not None:
                assert _has(w2.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled"), "W2 did not turn MGMT debugging on"
            w.send(f";W2,?SEQ,SAVE,HILSA,{a}")
            w.send(f";W2,?SEQ,SAVE,HILSB,{b}")
            time.sleep(2.5)
            m, m2 = w.dev.mark(), mark2()
            w.send("?MGMT,SEQ,2")
            time.sleep(0.3)
            w.send("?MGMT,SEQ,2")
            time.sleep(2.7)
            first, first_w2 = lines_since(m, "[MGMT:SEQ,2]"), asked(m2, NAMES_ASKED)
            m, m2 = w.dev.mark(), mark2()
            w.send("?MGMT,SEQ,2")
            time.sleep(3)
            second, second_w2 = lines_since(m, "[MGMT:SEQ,2]"), asked(m2, NAMES_ASKED)
            m, m2 = w.dev.mark(), mark2()
            for delay, key in ((0, "HILSA"), (0.15, "HILSB"), (0.15, "HILSA"), (0.5, "HILSA")):   # t2 +0, +150, +300, +800 ms
                time.sleep(delay)
                w.send(f"?MGMT,SEQGET,2,{key}")
            time.sleep(3.2)                                                                    # past t2+3.5 s
            window, window_w2 = lines_since(m, "[MGMT:SEQVAL,2]"), asked(m2, VALUE_ASKED)
            m, m2 = w.dev.mark(), mark2()
            w.send("?MGMT,SEQGET,2,HILSA")
            time.sleep(3)
            last, last_w2 = lines_since(m, "[MGMT:SEQVAL,2]"), asked(m2, VALUE_ASKED)
        finally:
            if w2 is not None:
                w2.run("?DEBUG,MGMT,OFF")
            w.send(";W2,?SEQ,CLEAR,HILSA")
            w.send(";W2,?SEQ,CLEAR,HILSB")
            time.sleep(1.5)
    key = {sa: "HILSA", sb: "HILSB"}
    for got in (window, last):
        stray = [x for x in got if x not in key]
        assert not stray, f"W1 printed SEQVAL lines that are neither stored value: {stray}"
    problems, lost, nlost = [], [], 0
    for what, answered, printed, want in (
            ("two SEQ requests 300 ms apart", first_w2, ["SEQ"] * len(first), ["SEQ"]),
            ("the spaced SEQ request", second_w2, ["SEQ"] * len(second), ["SEQ"]),
            ("the SEQGET window (the +800 ms HILSA repeat must drop)", window_w2, [key[x] for x in window],
             ["HILSA", "HILSB", "HILSA"]),
            ("the spaced HILSA request", last_w2, [key[x] for x in last], ["HILSA"])):
        problem, n = dedup_verdict(answered, printed, want)
        if problem:
            problems.append(f"{what}: {problem}")
        if n:
            nlost += n
            lost.append(f"{what}: {n} of W2's replies never reached W1")
    if lost:
        bench.note("inv.dedup: replies lost on the air (W2 sent them, W1 never printed them): " + "; ".join(lost))
    if nlost > 1:
        problems.append(f"{nlost} replies lost on the air ({'; '.join(lost)}): more than one in a run is not the odd "
                        f"lost frame")
    assert not problems, "; ".join(problems)


@test("inv.relay_arg_errors", "?MGMT,SEQ / SEQGET argument errors: key length ungated; target and format errors debug-only; absent or self targets silent", needs=["wcb1"], links=[])
def relay_arg_errors(bench):
    w = usb_wcb(bench)
    bad = []
    w.run("?DEBUG,MGMT,OFF")
    try:
        for cmd in ("?MGMT,SEQ,0", "?MGMT,SEQ,21", "?MGMT,SEQ,abc", "?MGMT,SEQ,1", "?MGMT,SEQ,7"):
            m = w.dev.mark()
            w.send(cmd)
            time.sleep(3)
            if [x for x in w.dev.since(m) if x.startswith("[MGMT")]:
                bad.append(f"{cmd} printed {[x for x in w.dev.since(m) if x.startswith('[MGMT')]}")
        for cmd in ("?MGMT,SEQGET,2,HILABCDEFGHIJKLM", "?MGMT,SEQGET,2,"):
            if not _has(w.run(cmd), "[MGMT] SEQGET: key must be 1-15 chars"):
                bad.append(f"{cmd} did not report the key length")
        for cmd in ("?MGMT,SEQGET,2", "?MGMT,SEQGET,0,HILX"):
            m = w.dev.mark()
            w.send(cmd)
            time.sleep(1.5)
            if [x for x in w.dev.since(m) if x.startswith("[MGMT")]:
                bad.append(f"{cmd} printed with debug off")
        m = w.dev.mark()
        w.send("?MGMT,SEQGET,2,HILABCDEFGHIJKL")
        try:
            w.dev.expect(r"^\[MGMT:SEQVAL,2\]HILABCDEFGHIJKL,NOTFOUND,", timeout=3, since=m)
        except AssertionError:
            bad.append("the 15-character SEQGET was not answered")
        assert _has(w.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled")
        for cmd, want in (("?MGMT,SEQ,0", "[MGMT] SEQ: invalid targetWCB"), ("?MGMT,SEQGET,2", "[MGMT] SEQGET: expected ?MGMT,SEQGET,<wcb>,<key>"),
                          ("?MGMT,SEQGET,0,HILX", "[MGMT] SEQGET: invalid targetWCB")):
            if not _has(w.run(cmd), want):
                bad.append(f"debug on: {cmd} lacks {want!r}")
    finally:
        w.run("?DEBUG,MGMT,OFF")
    assert not bad, "; ".join(bad)


def _seqget_line(w, wcb, key, timeout=3.0):
    m = w.dev.mark()
    w.send(f"?MGMT,SEQGET,{wcb},{key}")
    try:
        return w.dev.expect(rf"^\[MGMT:SEQVAL,{wcb}\]{re.escape(key)},", timeout=timeout, since=m).string.rstrip()
    except AssertionError:
        return None


@test("inv.seqget_formats", "?MGMT,SEQGET,2: OK with a value holding ^ and commas, NOTFOUND, and case-sensitive keys", needs=["wcb1"], links=[])
def seqget_formats(bench):
    w = usb_wcb(bench)
    a, b = marker("a"), marker("b")
    value = f";S2{a}^;S2{b},x"
    with config_guard(bench, 2):
        try:
            # ?MGMT,FRAG, because ;W2,?SEQ,SAVE would be split at the ^ by the sender (WCB.ino:2367-2389)
            w.send(f"?MGMT,FRAG,2,{nonce()[:4]},0,1,?SEQ,SAVE,HILSV,{value}")
            time.sleep(1.5)
            got = {}
            for key in ("HILSV", "HILNOPE", "hilsv"):
                # The reply is broadcast once and nothing acknowledges it, so a missing one is asked for again, as a
                # requester must (docs/SEQUENCE_INVENTORY.md 3a); a lost one failed this test in run 20260929-200710.
                for attempt in range(1, 4):
                    got[key] = _seqget_line(w, 2, key)
                    if got[key] is not None:
                        break
                    bench.note(f"SEQGET {key} try {attempt}/3: no [MGMT:SEQVAL,2] line (the request or the reply was "
                               f"lost); asking again")
                    time.sleep(RELAY_SPACING_S)
                time.sleep(0.3)
        finally:
            w.send(";W2,?SEQ,CLEAR,HILSV")
            time.sleep(1.5)
    assert got == {"HILSV": f"[MGMT:SEQVAL,2]HILSV,OK,{value}", "HILNOPE": "[MGMT:SEQVAL,2]HILNOPE,NOTFOUND,",
                   "hilsv": "[MGMT:SEQVAL,2]hilsv,NOTFOUND,"}, got


@test("inv.seqget_multichunk", "A ~900-character W2 sequence pushed as a six-chunk ?MGMT,FRAG comes back byte-exact in one [MGMT:SEQVAL] line", needs=["wcb1"], links=[])
def seqget_multichunk(bench):
    w = usb_wcb(bench)
    head = f";S2{marker('a')}^***"
    filler = "ABCdef0123*,"
    value = (head + filler * 80)[:900]
    payload = f"?SEQ,SAVE,HILMC,{value}"
    chunks = [payload[i:i + 179] for i in range(0, len(payload), 179)]     # a chunk over 179 chars is rejected (WCB.ino:2963)

    def push():
        sid = nonce()[:4]
        for i, chunk in enumerate(chunks):
            w.send(f"?MGMT,FRAG,2,{sid},{i},{len(chunks)},{chunk}")
            time.sleep(0.03)
        time.sleep(2)

    with config_guard(bench, 2):
        try:
            push()
            line = _seqget_line(w, 2, "HILMC", timeout=4)
            # Both legs are broadcast frags sent once, and each is lost on its own: no line means the request or W2's
            # 5-frag reply was lost (the value may well be stored; the 4 s wait already cleared W2's 1.5 s dedup),
            # NOTFOUND means the push was. A re-fetch after a lost reply can come back NOTFOUND, so rounds loop.
            for retry in range(1, 4):
                if line is not None and ",NOTFOUND," not in line:
                    break
                if line is None:
                    bench.note(f"multi-chunk retry {retry}/3: no [MGMT:SEQVAL,2] line (the request or W2's reply frags "
                               "were lost); fetching again")
                else:
                    bench.note(f"multi-chunk retry {retry}/3: W2 answered NOTFOUND (the ?MGMT,FRAG push was lost); pushing again")
                    push()
                    time.sleep(RELAY_SPACING_S)
                line = _seqget_line(w, 2, "HILMC", timeout=4)
        finally:
            w.send(";W2,?SEQ,CLEAR,HILMC")      # before the guard's snapshot: W2's config grows ~920 chars while stored
            time.sleep(1.5)
    assert line == f"[MGMT:SEQVAL,2]HILMC,OK,{value}", f"got {len(line or '')} chars: {(line or '')[:80]}..."


RELAY_MAX_PAYLOAD = 2912   # MGMT_MAX_CHUNKS 16 x (CONFIG_PAYLOAD_SIZE 183 - 1); '<key>,OK,<value>' must fit (WCB.ino:3845)
SEQ_LADDER = (2950, 2400, 2000, 1800, 1600, 1400, 1200, 1000, 900)
SEQGET_TRIES = 4           # ~1 in 4 multi-frag replies was lost on the bench (small sample): 4 tries leave ~1 %


@test("inv.seqget_largest", "The longest W1 sequence on a 2950..900 ladder round-trips exactly, locally and via W2 (TOOBIG past 2903); refusals are clean", needs=["wcb1"], links=[])
def seqget_largest(bench):
    """Replaces inv.seqget_toobig, whose >2903-character stored value W1 cannot hold (tracker #58). Two walls, neither
    a defect: ?SEQ,SAVE holds ~7 full-length String copies while it makes the 8th (WCB_Storage.cpp:675) against
    ~27 KB of byte-addressable heap (the old 'largest block 38900' was the 32-bit-only IRAM heap), and an NVS string
    cannot span a page, so a configured board refuses long values (WCB_Storage.cpp:703-706). The largest storable
    length moves as NVS fills, so this asserts behaviour, not a number: each refusal names its cause and leaves no
    phantom key, the stored value comes back byte-exact, and TOOBIG (WCB.ino:3845-3849) is checked by this same test
    on the day a value over 2903 characters stores."""
    w = usb_wcb(bench)
    key = "HILTB"
    head = f";S1{marker('a')}^***"      # a recall would send only the marker; the filler is a comment
    stored, refused, bad = None, [], []
    with config_guard(bench, 1), Console(bench, 2) as c2:
        _clear_seq(w, key)
        try:
            for n in SEQ_LADDER:
                value = head + "x" * (n - len(head))
                out = w.run(f"?SEQ,SAVE,{key},{value}", timeout=8)
                if _has(out, f"Stored: Key='{key}'"):
                    stored = (n, value)
                    break
                cause = ("NVS" if _has(out, f"Failed to store sequence '{key}'") else
                         "out of memory" if _has(out, "Out of memory: could not copy") else None)
                refused.append(f"{n} ({cause or 'no cause'})")
                if cause is None or _has(out, "cannot be empty"):   # an OOM copy used to read as an empty value
                    bad.append(f"{n}: the refusal does not name its cause: {[x[:100] for x in out]}")
                if key in _names(w)[2]:
                    bad.append(f"{n}: the refused save left {key} in ?SEQ,NAMES")
            bench.note(f"W1 stored {f'{stored[0]} characters' if stored else 'nothing'}; refused {', '.join(refused) or 'none'}")
            if stored:
                n, value = stored
                if _seqval(w, key) != f"[MGMT:SEQVAL,1]{key},OK,{value}":
                    bad.append(f"?SEQ,GET,{key} did not return the {n}-character value exactly")
                wm = w.dev.mark()
                if n + len(key) + 4 <= RELAY_MAX_PAYLOAD:
                    want, got = f"[MGMT:SEQVAL,1]{key},OK,{value}", None
                    # W1 answers in ceil((n + 9) / 182) broadcast frags, unACKed, in two passes (sendResultFrags,
                    # HIL_FIX_TRACKER #109), and W2 prints nothing until every frag is in: a frame lost from both
                    # passes is a silent miss by design, and the requester retries. 3 s a try clears W1's 1.5 s (requester,
                    # key) dedup (handleSeqValReqPacket). ?DEBUG,MGMT is RAM-only, so config_guard never sees it; its
                    # lines tell a lost request from lost frags when every try misses.
                    try:
                        # inside the try: a W1 timeout here must still turn W2's debug back off (its [WDP] chatter
                        # would otherwise land in later tests' output)
                        c2.send("?DEBUG,MGMT,ON")
                        w1_debug = _has(w.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled")
                        c0 = c2.mark()
                        for attempt in range(1, SEQGET_TRIES + 1):
                            cm = c2.mark()
                            w.send(f";W2,?MGMT,SEQGET,1,{key}")
                            try:
                                got = c2.expect(rf"\[MGMT:SEQVAL,1\]{key},", timeout=3, since=cm).string[len(c2.prefix):].rstrip()
                                break
                            except AssertionError:
                                bench.note(f"relayed fetch {attempt}/{SEQGET_TRIES}: W2 printed no [MGMT:SEQVAL,1] line"
                                           + ("; retrying" if attempt < SEQGET_TRIES else ""))
                        if got is None:
                            # W2 reaps a half-built session after CONFIG_SESSION_TIMEOUT_MS (10 s) and says so only
                            # under debug; each retry's new sessionId wiped the earlier ones silently
                            time.sleep(11)
                    finally:
                        c2.send("?DEBUG,MGMT,OFF")
                        w.run("?DEBUG,MGMT,OFF")
                    if got is None:
                        w1 = [x.rstrip() for x in w.dev.since(wm)]      # unprefixed: a relayed W2 line starts [TERM:2]
                        heard = sum(x.startswith(f"[MGMT] Sequence-value request '{key}' from WCB2 ") for x in w1)
                        sent = [x for x in w1 if x.startswith("[MGMT] Sent result frags (") and x.endswith(" to WCB2")]
                        reaped = any(x.startswith("[MGMT] Sequence-value session ") and "timed out" in x for x in c2.lines(c0))
                        if not w1_debug:
                            why = "W1 did not confirm ?DEBUG,MGMT,ON, so which side stopped is unknown"
                        elif sent:
                            why = (f"W1 heard {heard} of the {SEQGET_TRIES} requests and answered {len(sent)} ('{sent[-1]}'): "
                                   "the frags were lost on the air (the bench)"
                                   + ("; W2 reaped a half-built session, so some did arrive" if reaped else ""))
                        elif heard:
                            why = f"W1 heard {heard} of the {SEQGET_TRIES} requests but logged no 'Sent result frags' line (look at the firmware)"
                        elif any(x.startswith("[ETM] WCB2 failed to ACK ") for x in w1):     # ungated (WCB.ino ETM retry path)
                            why = "W1 logged no request from WCB2, and the ;W2 command itself was never ACKed: W2 never got it"
                        else:
                            why = "W1 logged no request from WCB2: the request never reached W1 (look at the firmware)"
                        bad.append(f"W2 printed no [MGMT:SEQVAL,1] line for the {n}-character value in {SEQGET_TRIES} tries; {why}")
                    # ?RTERM relays 160-character pieces (WCB_RemoteTerm.h:52): only the first can be compared,
                    # and it must be a whole piece - a bare '[MGMT:SEQVAL,1]HILTB,' would otherwise pass
                    elif got != want and not (c2.remote and len(got) >= min(len(want), 150) and want.startswith(got)):
                        bad.append(f"W2 relayed {len(got)} chars, not the {n}-character value: {got[:80]}...")
                else:
                    top = RELAY_MAX_PAYLOAD - len(key) - 4      # 2903
                    cm = c2.mark()
                    w.send(f";W2,?MGMT,SEQGET,1,{key}")
                    try:
                        w.dev.expect(rf"^\[MGMT\] Sequence '{key}' is {n} chars — too large to relay \(max {top}\)", timeout=5, since=wm)
                    except AssertionError:
                        bad.append(f"W1 did not report the {n}-character value too large to relay (max {top})")
                    try:
                        got = c2.expect(rf"\[MGMT:SEQVAL,1\]{key},", timeout=6, since=cm).string[len(c2.prefix):].rstrip()
                        if got != f"[MGMT:SEQVAL,1]{key},TOOBIG,":
                            bad.append(f"W2 printed {got[:80]!r}, not {key},TOOBIG,")
                    except AssertionError:
                        bad.append(f"W2 printed no [MGMT:SEQVAL,1] line for the {n}-character value")
        finally:
            _clear_seq(w, key)      # before the guard's snapshot, which would report the stored value as a leak
    assert stored and stored[0] >= 900, f"no length on the ladder stored; refused {', '.join(refused)}"
    assert not bad, "; ".join(bad)


@test("inv.mgmt_seq_w2_relay", "W2 as relay: ;W2,?MGMT,SEQ,1 yields W1's inventory, matching W1's ?SEQ,NAMES", needs=["wcb1"], links=[])
def mgmt_seq_w2_relay(bench):
    w = usb_wcb(bench)
    h1, n1, _ = _names(w)
    with Console(bench, 2) as c2:
        for attempt in range(1, 4):   # the reply frags are sent once, unacknowledged - retry like a requester (see _relay)
            cm = c2.mark()
            w.send(";W2,?MGMT,SEQ,1")
            try:
                c2.expect(rf"\[MGMT:SEQ,1\]{h1},{n1}\b", timeout=5, since=cm)
                break
            except AssertionError:
                if attempt == 3:
                    raise
                bench.note(f";W2,?MGMT,SEQ,1 try {attempt}: no reply (a lost frag); retrying")


# ============================================================ the sequence store: a file of its own (2026-09-29)
STORE_RX = re.compile(r"^Sequences: (\d+) in the sequence store, (\d+) of (\d+) bytes")
STORE_FULL = "the sequence store is full - see ?NVS"
NVS_BOOT = "Stored sequences are kept in the settings store this boot"


def _store(w):
    """(count, bytes, cap) from ?NVS's 'Sequences:' line, or None when W1 keeps its sequences in NVS (firmware before
    the store, or the store's fallback this boot)."""
    for x in w.run("?NVS", timeout=6):
        m = STORE_RX.match(x.rstrip())
        if m:
            return tuple(int(v) for v in m.groups())
    return None


def _store_or_skip(w):
    s = _store(w)
    if s is None:
        raise Skip("W1 keeps its sequences in NVS: no 'Sequences: ... in the sequence store' line in ?NVS")
    return s


@test("seq.store_accounting", "?NVS reports the sequence store: its count matches ?SEQ,NAMES; a save grows it by exactly key + value + 2 bytes (one '<key>,<value>' line), an edit in place by the difference and keeps the name's place, a re-save of the same value changes nothing, and a clear gives it all back; none of it touches NVS's stored_cmds; the store holds 32768 bytes", needs=["wcb1"], links=[])
def store_accounting(bench):
    """WCB_SeqStore.h: the sequences are the lines of one file, '<key>,<value>' in save order, on the 128 KB 'spiffs'
    partition; NVS keeps only the stored_cmds marker seq_mig_done."""
    w = usb_wcb(bench)
    _store_or_skip(w)
    k, v1, v2 = "HILSA", f";S1{marker('a')}", f";S1{marker('b')}^;S1{marker('c')}"
    problems = []
    with config_guard(bench, 1):
        _clear_seq(w, k)
        n0, b0, cap = _store_or_skip(w)
        e0 = _stored_cmds(w)
        _, count0, names0 = _names(w)
        if count0 != n0:
            problems.append(f"?NVS counts {n0} sequences, ?SEQ,NAMES {count0}")
        if cap != 32768:
            problems.append(f"the store holds {cap} bytes, not 32768")
        try:
            w.run(f"?SEQ,SAVE,{k},{v1}")
            s1 = _store(w)
            if s1[:2] != (n0 + 1, b0 + len(k) + len(v1) + 2):
                problems.append(f"after a save the store reads {s1[:2]}, not {(n0 + 1, b0 + len(k) + len(v1) + 2)}")
            if not _has(w.run(f"?SEQ,SAVE,{k},{v1}"), f"Stored: Key='{k}'"):
                problems.append("re-saving the same value was not confirmed")
            if _store(w) != s1:
                problems.append(f"re-saving the same value changed the store: {_store(w)} after {s1}")
            w.run(f"?SEQ,SAVE,{k},{v2}")
            s2 = _store(w)
            _, _, names2 = _names(w)
            if s2[:2] != (n0 + 1, b0 + len(k) + len(v2) + 2):
                problems.append(f"after an edit the store reads {s2[:2]}, not {(n0 + 1, b0 + len(k) + len(v2) + 2)}")
            if names2 != names0 + [k]:
                problems.append(f"after an edit the names end {names2[-3:]}: the edit moved '{k}'")
            if _seqval(w, k) != f"[MGMT:SEQVAL,1]{k},OK,{v2}":
                problems.append(f"after the edit {k} reads {_seqval(w, k)!r}")
            if _stored_cmds(w) != e0:
                problems.append(f"stored_cmds went from {e0} NVS entries to {_stored_cmds(w)}: a save touched NVS")
        finally:
            _clear_seq(w, k)
        if _store(w)[:2] != (n0, b0):
            problems.append(f"after the clear the store reads {_store(w)[:2]}, not {(n0, b0)}")
    assert not problems, "; ".join(problems)


@test("seq.store_full", "The sequence store fills to its 32768 bytes and no further: the save that does not fit says 'the sequence store is full' and leaves nothing listed or in ?backup; every stored one is in ?backup, whose chains stay whole; W2 still pulls W1's whole config in parts; a clear on the full store works and makes room for a save; all of it comes back out", needs=["wcb1", "wcb2"], links=[])
def store_full(bench):
    """SEQ_FILE_MAX (WCB_SeqStore.h). Under half the 128 KB partition, so replacing or removing a sequence (a second
    copy of the file, then a rename) always has room - the clear here runs on the full store; and small enough that
    the whole config still fits a config pull's 16 parts of 2880 bytes (48 KB answered ERROR TOOBIG). The fill values
    are a ;S0 marker and a comment, and nothing recalls them."""
    w = usb_wcb(bench)
    n0, b0, cap = _store_or_skip(w)
    keys, refused, problems = [], [], []
    with config_guard(bench, 1):
        try:
            # 1000-character values fill the store; smaller ones then find what is left. It ends when a 40-character
            # save is refused: 7 + 40 + 2 bytes that do not fit. Not bigger: a save costs about 7 times its length in
            # heap while it runs, and W1 hosting its access point has about 19 KB (SEQUENCE_INVENTORY.md 3b).
            n = 0
            for size in (1000, 200, 40):
                while n < 60:
                    n += 1
                    key = f"HILSF{n:02d}"
                    head = f";S0{marker('f')}^***"
                    out = w.run(f"?SEQ,SAVE,{key},{head}{'x' * (size - len(head))}", timeout=8)
                    if _has(out, f"Stored: Key='{key}'"):
                        keys.append(key)
                        continue
                    refused.append((key, size, next((x.rstrip() for x in out if "Failed to store sequence" in x), None)))
                    break
            full = _store(w)
            bench.note(f"stored {len(keys)} fill sequence(s); the store then {full}; refused "
                       f"{[(r[0], r[1]) for r in refused]}")
            if not refused or refused[-1][1] != 40:
                problems.append("the store never refused a 40-character save")
            if full[1] > cap:
                problems.append(f"the store holds {full[1]} bytes, over its {cap}")
            elif refused and full[1] + len(refused[-1][0]) + refused[-1][1] + 2 <= cap:
                problems.append(f"a {refused[-1][1]}-character save was refused with {cap - full[1]} bytes free")
            for key, size, line in refused:
                if not line or STORE_FULL not in line:
                    problems.append(f"the refused {size}-character save of {key} printed {line!r}")
            _, _, names = _names(w)
            listed = [r[0] for r in refused if r[0] in names]
            if listed:
                problems.append(f"refused key(s) listed by ?SEQ,NAMES: {listed}")
            # Every stored one in ?backup, and both one-line chains whole and CRC-valid at this size (tracker #90).
            backup = w.run("?backup", timeout=40)
            saved = {x.split(",")[2] for x in backup if x.upper().startswith("?SEQ,SAVE,") and x.count(",") >= 3}
            problems += backup_chain_problems(backup, "?backup with the store full")
            missing = [k for k in keys if k not in saved]
            if missing:
                problems.append(f"stored fill sequence(s) missing from ?backup: {missing}")
            leaked = [r[0] for r in refused if r[0] in saved]
            if leaked:
                problems.append(f"refused key(s) in ?backup: {leaked}")
            # The whole config still pulls: the reason the store stops at 32 KB.
            with Console(bench, 2) as c2:
                try:
                    r = pull_config(c2.dev, 1, parts=True, timeout=20)
                    got = sum(1 for k in keys if f"?SEQ,SAVE,{k}," in r.text)
                    bench.note(f"W2 pulled W1's config with the store full: {len(r.text)} characters in "
                               f"{len(r.part_lengths)} part(s), {got} of {len(keys)} fill sequences in it")
                    if got != len(keys):
                        problems.append(f"W2's pull of W1's config carries {got} of the {len(keys)} fill sequences")
                except PullRefused as e:
                    problems.append(f"W2's pull of W1's config with the store full was refused: {e}")
            # A clear on the full store, then a save into the room it made.
            if keys:
                k = keys.pop()
                if not _has(w.run(f"?SEQ,CLEAR,{k}"), f"Deleted stored command key: '{k}'"):
                    problems.append(f"clearing {k} on the full store was not confirmed")
                if not _has(w.run(f"?SEQ,SAVE,{k},;S0{marker('g')}"), f"Stored: Key='{k}'"):
                    problems.append(f"a save after the clear did not fit")
                keys.append(k)
        finally:
            for k in keys:
                w.run(f"?SEQ,CLEAR,{k}")
            for key, _, _ in refused:
                w.run(f"?SEQ,CLEAR,{key}")                   # harmless when it was never stored
        if _store(w)[:2] != (n0, b0):
            problems.append(f"after the clears the store reads {_store(w)[:2]}, not {(n0, b0)}")
    assert not problems, "; ".join(problems)


@test("seq.store_moves_from_nvs", "Sequences a boot on the store's NVS fallback saved (?DEBUG,SEQNVS) move into the store at the next boot: it says how many, a key both held takes NVS's value in its own place, a new one goes last, each reads back exactly, and stored_cmds is back to its size before; on the fallback boot the store's own sequences are unseen (2 reboots)", needs=["wcb1"], links=[])
def store_moves_from_nvs(bench):
    """seqStoreBegin -> moveNvsSequences (WCB_SeqStore.cpp). NVS only holds a sequence while the file exists when a
    fallback boot or an older firmware saved it - which is newer - so NVS wins a key both hold. The same move is what
    an update from firmware before the store runs once, for every sequence the board has."""
    w = usb_wcb(bench)
    _store_or_skip(w)
    a, a2, b = f";S1{marker('a')}", f";S1{marker('d')}", f";S1{marker('b')}"
    problems = []
    on_nvs = False
    with config_guard(bench, 1):
        _clear_seq(w, "HILMA", "HILMB")
        e0 = _stored_cmds(w)
        w.run(f"?SEQ,SAVE,HILMA,{a}")
        _, _, names0 = _names(w)
        try:
            if not _has(w.run("?DEBUG,SEQNVS"), "The next boot keeps stored sequences in NVS"):
                raise AssertionError("?DEBUG,SEQNVS was not accepted")
            m = w.reboot()
            on_nvs = True
            if not any(NVS_BOOT in x for x in w.dev.since(m)):
                problems.append("the ?DEBUG,SEQNVS boot did not say it keeps sequences in NVS")
            if _store(w) is not None:
                problems.append("?NVS reports the sequence store on the fallback boot")
            _, _, names = _names(w)
            if "HILMA" in names:
                problems.append(f"on the fallback boot ?SEQ,NAMES lists HILMA, which is in the store: {names}")
            w.run(f"?SEQ,SAVE,HILMA,{a2}")
            w.run(f"?SEQ,SAVE,HILMB,{b}")
            m = w.reboot()
            on_nvs = False
            boot = [x.rstrip() for x in w.dev.since(m)]
            if "[SEQ] Moved 2 sequence(s) from the settings store into the sequence store." not in boot:
                problems.append(f"the boot after did not report moving 2: {[x for x in boot if x.startswith('[SEQ]')]}")
            _, _, names1 = _names(w)
            if names1 != names0 + ["HILMB"]:
                problems.append(f"after the move the names end {names1[-3:]}, not {(names0 + ['HILMB'])[-3:]}")
            for k, want in (("HILMA", a2), ("HILMB", b)):
                if _seqval(w, k) != f"[MGMT:SEQVAL,1]{k},OK,{want}":
                    problems.append(f"{k} reads {_seqval(w, k)!r} after the move, not {want!r}")
            if _stored_cmds(w) != e0:
                problems.append(f"stored_cmds went from {e0} NVS entries to {_stored_cmds(w)}: NVS kept what moved")
        finally:
            if on_nvs:                                      # NVS's copies go before the boot that would move them
                _clear_seq(w, "HILMA", "HILMB")
                w.reboot()
            _clear_seq(w, "HILMA", "HILMB")
    assert not problems, "; ".join(problems)
