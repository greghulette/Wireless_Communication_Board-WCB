"""Stored sequences (?SEQ, ;C/;SEQ recall, legacy ?CS/?CE) and the sequence inventory relay (?MGMT,SEQ / SEQGET).

Built from the vars_seq_inventory specs; the console literals were re-checked against WCB.ino, WCB_Storage.cpp and
WCB_WDP.cpp. Rules from the specs:
- A top-level ;C<key> or ;SEQ<key> without ',L' ETM-broadcasts the recall to W2 and NaviCore (WCB.ino:6716-6719), so
  keys are HIL-prefixed and W2 sequences never write W2 S1 (its real Maestro).
- ';W2,?SEQ,SAVE,k,a^b' is split by the sender (W2 stores 'a', 'b' runs on W1, WCB.ino:2367-2389): values with '^'
  go to W2 through ?MGMT,FRAG.
- ?SEQ,CLEAR,ALL / ?CCLEAR wipe every sequence; that test is opt-in (bench.json "opt_in": ["seq_wipe"]) and replays
  the snapshot's ?SEQ,SAVE tokens in order, since key_list order drives both backup order and the SEQHASH.
- A target answers one SEQ_REQ per requester, and one SEQVAL_REQ per (requester, last key), per 1500 ms
  (WCB.ino:3593-3601, 3634-3646): identical relay requests are spaced 1.7 s apart.
- The mesh-client specs (seq.fanout_wire_probe, seq.peer_body_broadcasts) live with the probe mesh-mode tests.
"""
import re
import time

from hil.runner import Skip, test
from hil.wcb import chain_crc
from suites.common import Console, config_guard, link, marker, nonce, snapshot, usb_wcb

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


def _inventory_hash(strings):
    """FNV-1a 32 over key_list and then each value, each followed by a 0xFF separator (WCB_Storage.cpp:792-817)."""
    h = 0x811C9DC5
    for s in strings:
        for b in s.encode("utf-8"):
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
        h = ((h ^ 0xFF) * 16777619) & 0xFFFFFFFF
    return "%08X" % h


def _canonical(w, names):
    """key_list is 'a,b,' exactly when nothing (a migration, a hand edit) left it irregular; only then does a clear
    and replay reproduce the hash."""
    kl = _seqval(w, "key_list") or ""
    return kl == "[MGMT:SEQVAL,1]key_list,OK," + "".join(f"{n}," for n in names)


def _clear_seq(w, *keys):
    for k in keys:
        w.run(f"?SEQ,CLEAR,{k}")


def _opt_in(bench, flag, why):
    if flag not in bench.cfg.get("opt_in", []):
        raise Skip(f'opt-in: add "{flag}" to bench.json "opt_in" ({why})')


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


@test("seq.list_format", "?SEQ,LIST header, rows in save order, footer", needs=["wcb1"], links=[])
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


@test("seq.clear_all_empty_hash", "OPT-IN (seq_wipe): ?SEQ,CLEAR,ALL and ?CCLEAR wipe W1's sequences; an empty inventory hashes to 7A0B824E, not the documented 811C9DC5", needs=["wcb1"], links=[])
def clear_all_empty_hash(bench):
    """Doc bug: docs/SEQUENCE_INVENTORY.md:137 and :249, docs/WDP_DESIGN.md:109, WCB_Storage.h:164, WCB_WDP.h:89,
    tests/wdp_wire_test.cpp:69 and WCBClient (WCB_Client.h:404, README.md:536) give the empty hash as 811C9DC5, but
    sequenceInventoryHash() applies the 0xFF separator even to an empty key_list (WCB_Storage.cpp:792-798)."""
    _opt_in(bench, "seq_wipe", "wipes W1's sequences and replays them")
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


@test("seq.hash_algorithm", "?SEQ,NAMES hash == FNV-1a over the raw key_list then each value, each followed by a 0xFF separator", needs=["wcb1"], links=[])
def hash_algorithm(bench):
    w = usb_wcb(bench)
    a, b, c = marker("a"), marker("b"), marker("c")
    with config_guard(bench, 1):
        try:
            w.run(f"?SEQ,SAVE,HILH1,;S1{a}^;S1{b}")
            w.run(f"?SEQ,SAVE,HILH2,{c},x")
            kl = _seqval(w, "key_list")
            assert kl and kl.startswith("[MGMT:SEQVAL,1]key_list,OK,"), kl
            keys = kl[len("[MGMT:SEQVAL,1]key_list,OK,"):]
            values = []
            for k in (x.strip() for x in keys.split(",")):
                if k:
                    v = _seqval(w, k)
                    values.append(v[len(f"[MGMT:SEQVAL,1]{k},OK,"):] if v and ",OK," in v else "")
            h = _names(w)[0]
        finally:
            _clear_seq(w, "HILH1", "HILH2")
    assert keys.endswith(",") and "HILH1,HILH2," in keys, f"key_list {keys!r}"
    assert _inventory_hash([keys] + values) == h, f"W1 says {h}, host FNV-1a gives {_inventory_hash([keys] + values)}"


@test("seq.get_internal_keys", "Read-only: ?SEQ,GET also reads the NVS bookkeeping keys key_list and seq_mig_done, which NAMES hides", needs=["wcb1"], links=[])
def get_internal_keys(bench):
    """Minor finding: ?SEQ,GET does not filter internal keys (WCB.ino:5532-5539), so they read back as if they were
    sequences. Never recall ;Ckey_list (it runs the name list as a broadcast) or save to key_list."""
    w = usb_wcb(bench)
    _, _, names = _names(w)
    kl, mig = _seqval(w, "key_list"), _seqval(w, "seq_mig_done")
    bench.note(f"key_list canonical: {_canonical(w, names)}; seq_mig_done: {mig!r}")
    assert kl and kl.startswith("[MGMT:SEQVAL,1]key_list,OK,"), kl
    assert mig in ("[MGMT:SEQVAL,1]seq_mig_done,OK,", "[MGMT:SEQVAL,1]seq_mig_done,NOTFOUND,"), mig
    assert "key_list" not in names and "seq_mig_done" not in names, names


@test("seq.recall_forms", ";C / ;c / ;SEQ / ;seq recall with ,L / ,LOCAL / ,l; wrong-case and empty-key forms", needs=["wcb1"])
def recall_forms(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    a = marker("a")
    c_usage = "Invalid recall command. Use ;Ckey (or ;Ckey,L for local-only)."
    cases = [(";SEQHILR1,L", True, ["Recall stored command request received:SEQHILR1,L", "Recalling stored sequence command...",
                                     f"Recalling command for key 'HILR1': ;S1{a}"]),
             (";seqHILR1,L", True, []), (";cHILR1,LOCAL", True, []), (";CHILR1,l", True, []),
             (";SeqHILR1,L", False, ["Invalid Serial Command"]), (";sEQHILR1,L", False, ["Invalid Serial Command"]),
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


@test("seq.save_boundary", "A ?SEQ,SAVE value ends only at '^?'; '^;' and '^text' stay; a trailing '?' is kept; '?Seq,' is not exempt from the help trap", needs=["wcb1"])
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
            out = w.run(f"?Seq,SAVE,HILQ2,;S1{d}?")
            if not _has(out, "  Wireless Communication Board (WCB) - Command Reference") or _has(out, "Stored:"):
                bad.append(f"'?Seq,' was exempt from the help trap: {out[:3]}")
            if _seqval(w, "HILQ2") != "[MGMT:SEQVAL,1]HILQ2,NOTFOUND,":
                bad.append("'?Seq,SAVE' stored something")
        finally:
            _clear_seq(w, "HILB1", "HILB2", "HILQ", "HILQ2")
    assert not bad, "; ".join(bad)


REFUSE = "Sequence '{}' recalls itself (directly or in a loop) — refusing to expand it again. Break the cycle in the stored sequence."


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
    """Quirk: the guard compares keys with equalsIgnoreCase (WCB.ino:6736) while NVS keys are case-sensitive, so ;Chilrx
    inside HILRX is refused as recursion although no 'hilrx' exists."""
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


def _relay(w, cmd, pattern, space_key, timeout=3.0):
    """Send a relay request at least RELAY_SPACING_S after the last one with the same dedup key; return every matching
    W1 line (first match plus 0.5 s)."""
    last = _LAST_REQ.get(space_key)
    if last is not None:
        wait = RELAY_SPACING_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    m = w.dev.mark()
    w.send(cmd)
    _LAST_REQ[space_key] = time.monotonic()
    w.dev.expect(pattern, timeout=timeout, since=m)
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


@test("inv.dedup", "Relay dedup: one SEQ_REQ answer per requester per 1.5 s; SEQVAL dedup remembers only the last (requester, key) — timing-sensitive", needs=["wcb1"], links=[])
def dedup(bench):
    w = usb_wcb(bench)
    a, b = marker("a"), marker("b")

    def lines_since(m, prefix):
        return [x.rstrip() for x in w.dev.since(m) if x.startswith(prefix)]

    with config_guard(bench, 2):
        try:
            w.send(f";W2,?SEQ,SAVE,HILSA,{a}")
            w.send(f";W2,?SEQ,SAVE,HILSB,{b}")
            time.sleep(2.5)
            m = w.dev.mark()
            w.send("?MGMT,SEQ,2")
            time.sleep(0.3)
            w.send("?MGMT,SEQ,2")
            time.sleep(2.7)
            first = lines_since(m, "[MGMT:SEQ,2]")
            m = w.dev.mark()
            w.send("?MGMT,SEQ,2")
            time.sleep(3)
            second = lines_since(m, "[MGMT:SEQ,2]")
            m = w.dev.mark()
            for delay, key in ((0, "HILSA"), (0.15, "HILSB"), (0.15, "HILSA"), (0.5, "HILSA")):   # t2 +0, +150, +300, +800 ms
                time.sleep(delay)
                w.send(f"?MGMT,SEQGET,2,{key}")
            time.sleep(3.2)                                                                    # past t2+3.5 s
            window = lines_since(m, "[MGMT:SEQVAL,2]")
            m = w.dev.mark()
            w.send("?MGMT,SEQGET,2,HILSA")
            time.sleep(3)
            last = lines_since(m, "[MGMT:SEQVAL,2]")
        finally:
            w.send(";W2,?SEQ,CLEAR,HILSA")
            w.send(";W2,?SEQ,CLEAR,HILSB")
            time.sleep(1.5)
    sa, sb = f"[MGMT:SEQVAL,2]HILSA,OK,{a}", f"[MGMT:SEQVAL,2]HILSB,OK,{b}"
    assert len(first) == 1, f"two SEQ requests 300 ms apart answered {len(first)} times"
    assert len(second) == 1, "the spaced SEQ request was not answered"
    assert window.count(sa) == 2 and window.count(sb) == 1, f"SEQGET window {window} (the +800 ms HILSA repeat must drop)"
    assert last == [sa], f"the spaced HILSA request got {last}"


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
                got[key] = _seqget_line(w, 2, key)
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
            if line is None or ",NOTFOUND," in line:
                bench.note("multi-chunk push not stored on the first try (fragments are unACKed broadcasts); pushing again")
                push()
                time.sleep(RELAY_SPACING_S)
                line = _seqget_line(w, 2, "HILMC", timeout=4)
        finally:
            w.send(";W2,?SEQ,CLEAR,HILMC")      # before the guard's snapshot: W2's config grows ~920 chars while stored
            time.sleep(1.5)
    assert line == f"[MGMT:SEQVAL,2]HILMC,OK,{value}", f"got {len(line or '')} chars: {(line or '')[:80]}..."


@test("inv.seqget_toobig", "TOOBIG is reported explicitly: W2 relaying a >2903-character W1 value", needs=["wcb1"], links=[])
def seqget_toobig(bench):
    w = usb_wcb(bench)
    head = f";S1{marker('a')}^***"
    value = head + "x" * (2950 - len(head))
    with config_guard(bench, 1), Console(bench, 2) as c2:
        try:
            if _has(w.run(f"?SEQ,SAVE,HILTB,{value}", timeout=8), "Failed to store sequence 'HILTB'"):
                raise Skip("NVS rejected the 2950-character value")
            wm, cm = w.dev.mark(), c2.mark()
            w.send(";W2,?MGMT,SEQGET,1,HILTB")
            w.dev.expect(r"\[MGMT\] Sequence 'HILTB' is 2950 chars — too large to relay \(max 2903\)", timeout=5, since=wm)
            c2.expect(r"\[MGMT:SEQVAL,1\]HILTB,TOOBIG,", timeout=5, since=cm)
        finally:
            _clear_seq(w, "HILTB")


@test("inv.mgmt_seq_w2_relay", "W2 as relay: ;W2,?MGMT,SEQ,1 yields W1's inventory, matching W1's ?SEQ,NAMES", needs=["wcb1"], links=[])
def mgmt_seq_w2_relay(bench):
    w = usb_wcb(bench)
    h1, n1, _ = _names(w)
    with Console(bench, 2) as c2:
        cm = c2.mark()
        w.send(";W2,?MGMT,SEQ,1")
        c2.expect(rf"\[MGMT:SEQ,1\]{h1},{n1}\b", timeout=5, since=cm)
