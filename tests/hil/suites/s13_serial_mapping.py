"""?MAP,SERIAL — text and raw serial mapping, local and over the mesh.

Built from the verified serial_mapping specs. "(should)" tests assert the INTENDED behaviour where the
firmware has a probable bug and fail until it is fixed; their docstrings name the code. Every test
clears its mapping in a finally block, because a failed ?MAP update still leaves a live mapping in RAM
(WCB_Storage.cpp:1715-1742), and runs inside config_guard.

Never inject into W1S1 (KyberRemoteTask re-broadcasts it to the real Maestro), and never map to W2S1.
Restore with per-port ?MAP,SERIAL,CLEAR,S<n>, never CLEAR,ALL (it leaves broadcast output off).
"""
import re
import threading
import time

from hil.runner import Skip, test
from suites.common import (Console, Watch, config_guard, link, marker, padded, prime, require_tokens,
                           snapshot, token, usb_wcb)


def _has(lines, text):
    return any(text in x for x in lines)


def _list(w):
    return w.run("?MAP,SERIAL,LIST")


def _no_mappings(w):
    lines = _list(w)
    if not _has(lines, "No serial mappings configured"):
        raise Skip(f"W1 already has serial mappings: {lines}")


def _clear(w, *ports):
    for p in ports:
        w.run(f"?MAP,SERIAL,CLEAR,{p}")


# ============================================================ configuration
@test("map.parse_errors", "Malformed ?MAP,SERIAL commands print the exact error and leave no mapping behind", needs=["wcb1"], links=[])
def parse_errors(bench):
    w = usb_wcb(bench)
    _no_mappings(w)
    checks = [
        ("?MAP,SERIAL", "Invalid format. Use: ?MAP,SERIAL,Sx,dest  or  ?MAP,SERIAL,Sx,R,dest"),
        ("?MAP,SERIAL,S3", "Invalid format. Use: ?MAP,SERIAL,Sx,dest  or  ?MAP,SERIAL,Sx,R,dest"),
        ("?MAP,SERIAL,CLEAR", "Invalid format. Use: ?MAP,SERIAL,Sx,dest  or  ?MAP,SERIAL,Sx,R,dest"),
        ("?MAP,SERIAL,S0,S4", "Invalid input port. Must be 1-5"),
        ("?MAP,SERIAL,S6,S4", "Invalid input port. Must be 1-5"),
        ("?MAP,SERIAL,X3,S4", "Invalid format. Source must be Sx or SxR (e.g., S1, S3R)"),
        ("?MAP,SERIAL,CLEAR,S9", "Invalid port number. Must be 1-5"),
        ("?MAP,SERIAL,CLEAR,S4", "No mapping found for Serial4"),
        ("?MAP,FOO", "Invalid MAP command. Use: ?MAP ?"),
    ]
    with config_guard(bench, 1):
        bad = []
        for cmd, want in checks:
            out = w.run(cmd)
            if not _has(out, want) or _has(out, "Auto-"):
                bad.append(f"{cmd!r} -> {out}")
        if not _has(_list(w), "No serial mappings configured"):
            bad.append("a mapping was left behind")
        assert not bad, "; ".join(bad)


@test("map.set_flags_list_backup", "A mapping auto-clears the input port's broadcast flags; LIST, ?config and ?backup show it; re-issue is idempotent", needs=["wcb1"], links=[])
def set_flags_list_backup(bench):
    w = usb_wcb(bench)
    _no_mappings(w)
    require_tokens(bench, 1, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    with config_guard(bench, 1):
        try:
            out = w.run("?MAP,SERIAL,S3,S4")
            order = ["Auto-enabled broadcast input blocking on Serial3", "Auto-disabled broadcast output on Serial3",
                     "Serial mapping set: Serial3 -> 1 destination(s)"]
            idx = [next((i for i, x in enumerate(out) if want in x), -1) for want in order]
            assert -1 not in idx and idx == sorted(idx), f"set output out of order: {out}"
            assert any(re.search(r"Mapping \d+: Serial3 -> S4", x) for x in _list(w)), "LIST lacks the mapping"
            cfg = w.run("?config")
            assert any(re.search(r"Mapping \d+: Serial3 -> S4", x) for x in cfg), "?config lacks the mapping row"
            assert any(x.startswith("    Serial3 input blocked") for x in cfg), "?config lacks 'Serial3 input blocked'"
            tokens = snapshot(bench, 1)
            for t in ("?BCAST,OUT,S3,OFF", "?BCAST,IN,S3,OFF", "?MAP,SERIAL,S3,S4"):
                assert t in tokens, f"?backup lacks {t}"
            again = w.run("?MAP,SERIAL,S3,S4")
            assert _has(again, "Serial mapping set: Serial3 -> 1 destination(s)") and not _has(again, "Auto-") \
                and not _has(again, "already exists"), f"re-issue was not idempotent: {again}"
            out = w.run("?MAP,SERIAL,CLEAR,S3")
            for want in ("Auto-disabled broadcast input blocking on Serial3", "Auto-enabled broadcast output on Serial3",
                         "Serial mapping removed for Serial3"):
                assert _has(out, want), f"CLEAR lacks {want!r}: {out}"
            assert _has(_list(w), "No serial mappings configured")
        finally:
            _clear(w, "S3")


# ============================================================ text mode
@test("map.text_local_exact", "Text mode: an un-prefixed line is trimmed and delivered once with CR only; nothing else goes out, mesh included", needs=["wcb1"])
def text_local_exact(bench):
    s1, s2, s3, s4, s5 = (link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 1, "S3"),
                          link(bench, 1, "S4"), link(bench, 1, "S5"))
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,S4"), "Serial mapping set: Serial2 -> 1 destination(s)")
            w.run("?DEBUG,ETM,ON")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s1, s3, s4, s5)
            m = w.dev.mark()
            s2.send(b"\r\n\r\n")
            time.sleep(0.3)
            s2.send(f"  {t}  \r\n".encode())
            watch.expect(s4, t.encode() + b"\r", timeout=2)
            watch.silent(s1, s3, s5, window=1.5)
            assert watch.got(s4) == t.encode() + b"\r", f"S4 got {watch.got(s4)!r}"
            assert not s4.errors(watch.marks[s4.key]), "framing errors on S4"
            assert not any(t in x for x in w.dev.since(m)), "the mapped line also went out as a broadcast"
        finally:
            w.run("?DEBUG,ETM,OFF")
            _clear(w, "S2")


@test("map.text_multi_dest", "Text mode with local, S0 and remote destinations sends one copy to each, in list order", needs=["wcb1"])
def text_multi_dest(bench):
    s1, s2, s3, s4, s5 = (link(bench, 1, "S1"), link(bench, 1, "S2"), link(bench, 1, "S3"),
                          link(bench, 1, "S4"), link(bench, 1, "S5"))
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,S3,S0,W2S2,S4,S5"), "Serial mapping set: Serial2 -> 5 destination(s)")
            assert any(re.search(r"Mapping \d+: Serial2 -> S3, S0, W2S2, S4, S5", x) for x in _list(w))
            assert "?MAP,SERIAL,S2,S3,S0,W2S2,S4,S5" in snapshot(bench, 1)
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s1, s3, s4, s5, w2s2)
            m = w.dev.mark()
            s2.send(t.encode() + b"\r")
            for l in (s3, s4, s5, w2s2):
                watch.expect(l, t.encode() + b"\r", timeout=3)
            watch.silent(s1, window=1.0)
            for l in (s3, s4, s5, w2s2):
                assert watch.got(l).count(t.encode()) == 1, f"{l.key} got {watch.got(l)!r}"
            usb_hits = [x for x in w.dev.since(m) if x.strip() == t]
            assert len(usb_hits) == 1, f"expected one USB line, got {len(usb_hits)}"
            times = [l.time_of(t.encode(), watch.marks[l.key]) for l in (s3, s4, s5)]
            bench.note(f"text_multi_dest burst starts S3/S4/S5 (probe ms): {times}")
        finally:
            _clear(w, "S2")


@test("map.text_remote_etm", "A remote text destination goes out as an ETM unicast ';S<p><text>' and is ACKed", needs=["wcb1"])
def text_remote_etm(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,W2S2"), "Serial mapping set: Serial2 -> 1 destination(s)")
            w.run("?DEBUG,ETM,ON")
            before = w.etm_board_stats().get(2)
            if not before or not before["online"]:
                raise Skip("WCB2 is not online")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s3, s4, s5, w2s2)
            m = w.dev.mark()
            s2.send(t.encode() + b"\r")
            w.dev.expect(rf"^\[ETM\] Sent seq \d+: ;S2{t}$", timeout=2, since=m)
            watch.expect(w2s2, t.encode() + b"\r", timeout=2)
            watch.silent(s3, s4, s5, window=1.0)
            after = w.etm_board_stats()[2]
            assert after["ackd"] >= before["ackd"] + 1 and after["failed"] == before["failed"], f"{before} -> {after}"
        finally:
            w.run("?DEBUG,ETM,OFF")
            _clear(w, "S2")


@test("map.text_prefixed_and_chains", "Text mode forwards only un-prefixed tokens: ? and ; still run, *** is ignored, ^ splits", needs=["wcb1"])
def text_prefixed_and_chains(bench):
    s2, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    a, b, c, d, e, f = (marker(x) for x in "abcdef")
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,S4")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s4)
            m = w.dev.mark()
            s2.send(b"?MAP,SERIAL,LIST\r")
            w.dev.expect(r"Mapping \d+: Serial2 -> S4", timeout=2, since=m)
            watch.silent(s4, window=0.8)
            watch = Watch(s4, s5)
            s2.send(f";S5{a}\r".encode())
            watch.expect(s5, a.encode() + b"\r", timeout=2)
            watch.silent(s4, window=0.8)
            watch = Watch(s4)
            s2.send(f"{b}?\r".encode())
            watch.expect(s4, f"{b}?\r".encode(), timeout=2)
            watch = Watch(s4)
            m = w.dev.mark()
            s2.send(f"***{c}\r".encode())
            w.dev.expect(rf"^Ignored chain command: \*\*\*{c}$", timeout=2, since=m)
            watch.silent(s4, window=0.8)
            watch = Watch(s4, s5)
            s2.send(f"{d}^;S5{e}^{f}\r".encode())
            watch.expect(s4, f"{d}\r{f}\r".encode(), timeout=2)
            watch.expect(s5, e.encode() + b"\r", timeout=2)
        finally:
            _clear(w, "S2")


@test("map.text_self_wcb", "(should) A text mapping to this board's own W<n>S<p> delivers locally", needs=["wcb1"])
def text_self_wcb(bench):
    """Probable firmware bug: processBroadcastCommand treats only destWCB == 0 as local (WCB.ino:6911-6927), so
    W<own>S<p> goes out as an ESP-NOW unicast to the board's own MAC and is dropped. Raw mode treats W<own> as
    local (WCB.ino:7366). The wiki example ?MAP,SERIAL,S4,W1S1,... (Command-Reference.md:188) relies on it."""
    s2, s4 = link(bench, 1, "S2"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_mappings(w)
    me = bench.usb_wcb_number()
    t = marker()
    with config_guard(bench, 1):
        try:
            assert _has(w.run(f"?MAP,SERIAL,S2,W{me}S4"), "Serial mapping set: Serial2 -> 1 destination(s)")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s4)
            m = w.dev.mark()
            s2.send(t.encode() + b"\r")
            time.sleep(2.0)
            failed = [x for x in w.dev.since(m) if x.startswith("[ETM] Send failed seq")]
            bench.note(f"text_self_wcb send-failure lines: {failed}")
            assert t.encode() + b"\r" in watch.got(s4), f"W{me}S4 never received the line ({failed or 'no send-failure line'})"
        finally:
            _clear(w, "S2")


@test("map.text_chksm_limit", "Under CHKSM a remote text destination refuses lines over 184 chars; the local one still gets them", needs=["wcb1"])
def text_chksm_limit(bench):
    s2, s4 = link(bench, 1, "S2"), link(bench, 1, "S4")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    require_tokens(bench, 1, "?ETM,CHKSM,ON")
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,W2S2,S4"), "Serial mapping set: Serial2 -> 2 destination(s)")
            prime(s2)
            time.sleep(0.3)
            line_a = padded("A", 184)
            watch = Watch(s4, w2s2)
            s2.send(line_a.encode() + b"\r")
            watch.expect(w2s2, line_a.encode() + b"\r", timeout=3)
            watch.expect(s4, line_a.encode() + b"\r", timeout=3)
            time.sleep(2.0)
            line_b = padded("B", 185)
            watch = Watch(s4, w2s2)
            m = w.dev.mark()
            s2.send(line_b.encode() + b"\r")
            w.dev.expect(r"^\[ETM\] Command 188 chars exceeds the 187-char limit under \?ETM,CHKSM", timeout=3, since=m)
            watch.expect(s4, line_b.encode() + b"\r", timeout=3)
            watch.silent(w2s2, window=2.0)
        finally:
            _clear(w, "S2")


@test("map.text_inbound_from_w2", "A mapping set on WCB2 over the mesh forwards W2 S<q> text to W1 S4, and shows in ?MGMT,PULL", needs=["wcb1"], links=["W2S3|W2S4|W2S5", "W1S4"])
def text_inbound_from_w2(bench):
    s4 = link(bench, 1, "S4")
    tokens = bench.config_tokens(2, refresh=True)
    busy = " ".join(t for t in tokens if t.upper().startswith(("?HCR", "?MP3", "?DFP", "?MAESTRO,M", "?WLED")))
    q = next((p for p in ("S3", "S4", "S5") if bench.links.usable(2, p, send=True) and f"?BCAST,OUT,{p},ON" in tokens
              and f"?BCAST,IN,{p},ON" in tokens and f"W2{p}" not in busy and f"S{p[1]}:" not in busy), None)
    if not q:
        raise Skip("no wired W2 S3-S5 port with broadcast flags ON and no device")
    src = link(bench, 2, q)
    t = marker()
    with config_guard(bench, 2), Console(bench, 2) as c2:
        try:
            m = c2.send(f"?MAP,SERIAL,{q},W1S4")
            c2.expect(rf"Serial mapping set: Serial{q[1]} -> 1 destination", timeout=4, since=m)
            time.sleep(1.0)
            pulled = snapshot(bench, 2)
            for want in (f"?MAP,SERIAL,{q},W1S4", f"?BCAST,OUT,{q},OFF", f"?BCAST,IN,{q},OFF"):
                assert want in pulled, f"W2 config lacks {want}"
            prime(src)
            time.sleep(0.3)
            watch = Watch(s4)
            src.send(t.encode() + b"\r")
            watch.expect(s4, t.encode() + b"\r", timeout=3)
        finally:
            c2.send(f"?MAP,SERIAL,CLEAR,{q}")
            time.sleep(1.5)


@test("map.text_remote_s0", "A text mapping to W2S0 prints the line on WCB2's console", needs=["wcb1"])
def text_remote_s0(bench):
    s2 = link(bench, 1, "S2")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,W2S0"), "Serial mapping set: Serial2 -> 1 destination(s)")
            with Console(bench, 2) as c2:
                prime(s2)
                time.sleep(0.3)
                watch = Watch(w2s2)
                m = c2.mark()
                s2.send(t.encode() + b"\r")
                c2.expect(rf"{t}$", timeout=4, since=m)
                watch.silent(w2s2, window=1.0)
        finally:
            _clear(w, "S2")


@test("map.text_soft_inputs", "Text mode on the software-serial inputs S3, S4 and S5 forwards to hardware S2", needs=["wcb1"])
def text_soft_inputs(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    with config_guard(bench, 1):
        try:
            for p in ("S3", "S4", "S5"):
                assert _has(w.run(f"?MAP,SERIAL,{p},S2"), f"Serial mapping set: Serial{p[1]} -> 1 destination(s)")
            prime(s3, s4, s5)
            time.sleep(0.5)
            for src in (s3, s4, s5):
                t = marker(src.port[1])
                others = [l for l in (s3, s4, s5) if l is not src]
                watch = Watch(s2, *others)
                src.send(t.encode() + b"\r")
                watch.expect(s2, t.encode() + b"\r", timeout=2)
                watch.silent(*others, window=1.0)
                assert watch.got(s2).count(t.encode()) == 1, f"S2 got {watch.got(s2)!r}"
        finally:
            _clear(w, "S3", "S4", "S5")


@test("map.replace_not_append", "A second ?MAP,SERIAL for the same input replaces its destination list", needs=["wcb1"])
def replace_not_append(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,S3,S4")
            out = w.run("?MAP,SERIAL,S2,S5")
            assert _has(out, "Serial mapping set: Serial2 -> 1 destination(s)") and not _has(out, "Auto-"), out
            assert any(re.search(r"Mapping \d+: Serial2 -> S5$", x.rstrip()) for x in _list(w))
            maps = [x for x in snapshot(bench, 1) if x.startswith("?MAP,SERIAL,")]
            assert maps == ["?MAP,SERIAL,S2,S5"], f"backup mappings: {maps}"
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s3, s4, s5)
            s2.send(t.encode() + b"\r")
            watch.expect(s5, t.encode() + b"\r", timeout=2)
            watch.silent(s3, s4, window=1.0)
        finally:
            _clear(w, "S2")


@test("map.duplicate_destinations", "Duplicate destinations in one command are skipped (W0Sx = Sx, case-insensitive)", needs=["wcb1"])
def duplicate_destinations(bench):
    s2, s4 = link(bench, 1, "S2"), link(bench, 1, "S4")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            out = w.run("?MAP,SERIAL,S2,S4,S4,W0S4,W2S2,w2s2")
            assert sum("Mapping already exists: Serial2 -> S4 - skipping duplicate" in x for x in out) == 2, out
            assert sum("Mapping already exists: Serial2 -> W2S2 - skipping duplicate" in x for x in out) == 1, out
            assert _has(out, "Serial mapping set: Serial2 -> 2 destination(s)"), out
            assert any(re.search(r"Mapping \d+: Serial2 -> S4, W2S2", x) for x in _list(w))
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s4, w2s2)
            s2.send(t.encode() + b"\r")
            watch.expect(s4, t.encode() + b"\r", timeout=2)
            watch.expect(w2s2, t.encode() + b"\r", timeout=3)
            time.sleep(0.5)
            assert watch.got(s4).count(t.encode()) == 1 and watch.got(w2s2).count(t.encode()) == 1
        finally:
            _clear(w, "S2")


@test("map.destination_validation", "Destination parser: range and format errors, a stripped R suffix, and 'W2S' becoming W2S0 (characterisation)", needs=["wcb1"], links=[])
def destination_validation(bench):
    """Finding: no digit validation — String::toInt() turns an empty port into 0 and S-1 into 255
    (WCB_Storage.cpp:1779-1800)."""
    w = usb_wcb(bench)
    _no_mappings(w)
    with config_guard(bench, 1):
        try:
            out = w.run("?MAP,SERIAL,S2,S4,W21S1,S6,S-1,X9,W2,S4R,W2S")
            wants = ["Invalid destination: WCB 21 Serial 1", "Invalid destination: WCB 0 Serial 6",
                     "Invalid destination: WCB 0 Serial 255", "Invalid destination format: X9",
                     "Invalid destination format: W2", "Mapping already exists: Serial2 -> S4 - skipping duplicate",
                     "Serial mapping set: Serial2 -> 2 destination(s)"]
            missing = [x for x in wants if not _has(out, x)]
            assert not missing, f"missing {missing}: {out}"
            assert any(re.search(r"Mapping \d+: Serial2 -> S4, W2S0", x) for x in _list(w))
            assert "?MAP,SERIAL,S2,S4,W2S0" in snapshot(bench, 1)
        finally:
            _clear(w, "S2")


@test("map.max_ten", "An 11th destination is dropped without any message (characterisation)", needs=["wcb1"], links=[])
def max_ten(bench):
    w = usb_wcb(bench)
    _no_mappings(w)
    with config_guard(bench, 1):
        try:
            out = w.run("?MAP,SERIAL,S2,W3S1,W4S1,W5S1,W6S1,W7S1,W8S1,W9S1,W10S1,W11S1,W12S1,W13S1")
            assert _has(out, "Serial mapping set: Serial2 -> 10 destination(s)") and not _has(out, "W13"), out
            want = "Serial2 -> W3S1, W4S1, W5S1, W6S1, W7S1, W8S1, W9S1, W10S1, W11S1, W12S1"
            assert any(want in x for x in _list(w)), _list(w)
        finally:
            _clear(w, "S2")


@test("map.failed_update_keeps_mapping", "(should) An update with only invalid destinations leaves the existing mapping working", needs=["wcb1"])
def failed_update_keeps_mapping(bench):
    """Probable firmware bug: addSerialMonitorMapping zeroes outputCount before parsing (WCB_Storage.cpp:1719) but
    saves only when outputsAdded > 0 (1833-1842): a failed update leaves a zero-destination mapping in RAM that
    swallows every line, ?backup emits an unreplayable '?MAP,SERIAL,S2', and a reboot restores the old list."""
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,S4")
            out = w.run("?MAP,SERIAL,S2,X9")
            assert _has(out, "Invalid destination format: X9"), out
            problems = []
            if "?MAP,SERIAL,S2" in snapshot(bench, 1):
                problems.append("?backup now holds the unreplayable token '?MAP,SERIAL,S2'")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s3, s4, s5)
            s2.send(t.encode() + b"\r")
            time.sleep(1.5)
            if t.encode() not in watch.got(s4):
                problems.append("S2 went silent: the mapping to S4 no longer delivers")
            assert not problems, "; ".join(problems)
        finally:
            _clear(w, "S2")


@test("map.bare_raw_flag", "(should) '?MAP,SERIAL,S2,R' with no destination does not make S2 deaf", needs=["wcb1"])
def bare_raw_flag(bench):
    """Probable firmware bug: '?MAP,SERIAL,S2,R' becomes 'S2R,' (WCB.ino:5032-5034) and creates an active raw
    mapping with no outputs; serialCommandTask stops reading S2 (WCB.ino:7295) while the only message is
    'No new destinations added', and ?backup replays it as '?MAP,SERIAL,S2,R'."""
    s2, s5 = link(bench, 1, "S2"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    a = marker("a")
    with config_guard(bench, 1):
        try:
            out = w.run("?MAP,SERIAL,S2,R")
            bench.note(f"?MAP,SERIAL,S2,R -> {out}")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s5)
            s2.send(f";S5{a}\r".encode())
            time.sleep(1.5)
            assert a.encode() + b"\r" in watch.got(s5), "commands typed on S2 stopped running after '?MAP,SERIAL,S2,R'"
        finally:
            _clear(w, "S2")


# ============================================================ raw mode
@test("map.raw_local_verbatim", "Raw mode forwards bytes exactly: no CR added, no trimming, no command parsing", needs=["wcb1"])
def raw_local_verbatim(bench):
    s2, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    n = marker()
    data = bytes.fromhex("00010D0A203F3B5E7F80AAFF") + f";S5{n}\r".encode() + b"?MAP,SERIAL,LIST\r"
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,R,S4"), "Serial mapping set: Serial2 (RAW) -> 1 destination(s)")
            assert "?MAP,SERIAL,S2,R,S4" in snapshot(bench, 1)
            watch = Watch(s4, s5)
            m = w.dev.mark()
            s2.send(data)
            watch.expect(s4, data, timeout=2)
            watch.silent(s5, window=1.0)
            assert watch.got(s4) == data, f"S4 got {watch.got(s4).hex(' ')}"
            assert not s4.errors(watch.marks[s4.key]), "framing errors on S4"
            assert not any(re.search(r"Mapping \d+:", x) for x in w.dev.since(m)), "LIST ran from a raw-mapped port"
        finally:
            _clear(w, "S2")


@test("map.raw_debug_lines", "?DEBUG,RAW,ON prints one hex line per chunk for local and relay sends", needs=["wcb1"])
def raw_debug_lines(bench):
    s2, s4 = link(bench, 1, "S2"), link(bench, 1, "S4")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,R,S4,W2S2"), "Serial mapping set: Serial2 (RAW) -> 2 destination(s)")
            w.run("?DEBUG,RAW,ON")
            watch = Watch(s4, w2s2)
            m = w.dev.mark()
            s2.send(b"\x41")
            w.dev.expect(r"^\[RAW\] S2 → S4 \(local\)  1 byte", timeout=2, since=m)
            w.dev.expect(r"^\[RAW\] S2 → W2S2 \(relay\)  1 byte", timeout=2, since=m)
            time.sleep(0.5)
            m2 = w.dev.mark()
            s2.send(bytes.fromhex("0A0B0C"))
            time.sleep(1.0)
            for kind in (r"S2 → S4 \(local\)", r"S2 → W2S2 \(relay\)"):
                hexes = []
                for x in w.dev.since(m2):
                    hit = re.match(rf"^\[RAW\] {kind}  (\d+) byte(?:\(s\)|s)?: (.*)$", x)
                    if hit:
                        vals = hit.group(2).split()
                        assert int(hit.group(1)) == len(vals), f"count/hex mismatch: {x}"
                        hexes += vals
                assert " ".join(hexes).upper() == "0A 0B 0C", f"{kind} chunks joined to {hexes}"
            want = bytes.fromhex("410A0B0C")
            watch.expect(s4, want, timeout=2)
            watch.expect(w2s2, want, timeout=3)
        finally:
            w.run("?DEBUG,RAW,OFF")
            _clear(w, "S2")


@test("map.raw_remote_115200_exact", "Raw to a remote port at 115200: relay chunks <= 64 bytes add up and 200 bytes arrive in order", needs=["wcb1"])
def raw_remote_115200_exact(bench):
    s2 = link(bench, 1, "S2")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    data = bytes(i & 0xFF for i in range(200))
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?BAUD,S2,115200"), "Baud rate for Serial2 updated to 115200")
            s2.listen(115200)
            assert _has(w.run("?MAP,SERIAL,S2,R,W2S2"), "Serial mapping set: Serial2 (RAW) -> 1 destination(s)")
            w.run("?DEBUG,RAW,ON")
            watch = Watch(w2s2)
            m = w.dev.mark()
            s2.send(data)
            watch.expect(w2s2, data, timeout=4)
            time.sleep(0.5)
            sizes = [int(h.group(1)) for h in (re.match(r"^\[RAW\] S2 → W2S2 \(relay\)  (\d+) byte", x) for x in w.dev.since(m)) if h]
            assert sizes and max(sizes) <= 64 and sum(sizes) == 200, f"relay chunk sizes {sizes}"
            assert watch.got(w2s2) == data, "W2S2 bytes differ from what was sent"
        finally:
            w.run("?DEBUG,RAW,OFF")
            _clear(w, "S2")
            w.run("?BAUD,S2,9600")


@test("map.raw_remote_bursts_no_loss", "(should) Raw bursts to a remote port at 115200 lose no bytes", needs=["wcb1"])
def raw_remote_bursts_no_loss(bench):
    """Possible firmware bug: sendESPNowRawSerial advances past a chunk even when esp_now_send fails (WCB.ino:
    2855-2866); the NO_MEM comment promises a retry that never happens, and failures print only under ?DEBUG,ON."""
    s2 = link(bench, 1, "S2")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    bursts = [bytes(((i * 7 + k) & 0xFF) for i in range(200)) for k in range(10)]
    with config_guard(bench, 1):
        try:
            w.run("?BAUD,S2,115200")
            s2.listen(115200)
            w.run("?MAP,SERIAL,S2,R,W2S2")
            watch = Watch(w2s2)
            for b in bursts:
                s2.send(b)
                time.sleep(0.05)
            expected = b"".join(bursts)
            try:
                watch.expect(w2s2, expected, timeout=6)
            except AssertionError:
                got = watch.got(w2s2)
                raise AssertionError(f"received {len(got)} of {len(expected)} bytes; whole missing chunks point at the "
                                     f"NO_MEM drop (WCB.ino:2855-2866)")
        finally:
            _clear(w, "S2")
            w.run("?BAUD,S2,9600")


@test("map.raw_self_wcb_local", "Raw mode with this board's own W<n>S<p> is written locally", needs=["wcb1"])
def raw_self_wcb_local(bench):
    s2, s4 = link(bench, 1, "S2"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_mappings(w)
    me = bench.usb_wcb_number()
    t = marker()
    with config_guard(bench, 1):
        try:
            assert _has(w.run(f"?MAP,SERIAL,S2,R,W{me}S4"), "Serial mapping set: Serial2 (RAW) -> 1 destination(s)")
            w.run("?DEBUG,RAW,ON")
            watch = Watch(s4)
            m = w.dev.mark()
            s2.send(t.encode())
            watch.expect(s4, t.encode(), timeout=2)
            time.sleep(0.5)
            lines = [x for x in w.dev.since(m) if x.startswith("[RAW]")]
            assert lines and all("(local)" in x for x in lines), f"debug lines: {lines}"
        finally:
            w.run("?DEBUG,RAW,OFF")
            _clear(w, "S2")


@test("map.raw_s0_local", "Raw to local S0 writes the bytes to USB", needs=["wcb1"])
def raw_s0_local(bench):
    s2 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2,R,S0"), "Serial mapping set: Serial2 (RAW) -> 1 destination(s)")
            m = w.dev.mark()
            s2.send(t.encode() + b"\r\n")
            w.dev.expect(re.escape(t), timeout=2, since=m)
        finally:
            _clear(w, "S2")


@test("map.raw_remote_s0_refused", "(should) A raw mapping to a remote S0 is refused when it is configured", needs=["wcb1"], links=[])
def raw_remote_s0_refused(bench):
    """Probable firmware bug: the parser accepts W<n>S0 for raw mappings (WCB_Storage.cpp:1797-1800) but the
    receiver rejects every target-97 chunk with port < 1 (WCB.ino:4589), so the mapping can never deliver."""
    w = usb_wcb(bench)
    _no_mappings(w)
    with config_guard(bench, 1):
        try:
            out = w.run("?MAP,SERIAL,S2,R,W2S0")
            assert not _has(out, "Serial mapping set"), f"an undeliverable raw W2S0 mapping was accepted: {out}"
        finally:
            _clear(w, "S2")


@test("map.unreachable_board", "A destination on a non-peer board fails visibly: raw under ?DEBUG,ON, text as an ungated ETM line", needs=["wcb1"])
def unreachable_board(bench):
    s2 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    tokens = bench.config_tokens(1, refresh=True)
    if token(tokens, "?WCBQ,") not in ("?WCBQ,1", "?WCBQ,2"):
        raise Skip("WCBQ is above 2, so WCB3 may be a peer")
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,R,W3S1")
            w.run("?DEBUG,ON")
            m = w.dev.mark()
            s2.send(b"\x41")
            raw = w.dev.expect(r"^ESP-NOW raw serial send failed! Error: (-?\d+)", timeout=2, since=m)
            w.run("?DEBUG,OFF")
            w.run("?MAP,SERIAL,S2,W3S1")
            prime(s2)
            time.sleep(0.3)
            m = w.dev.mark()
            s2.send(marker().encode() + b"\r")
            text = w.dev.expect(r"^\[ETM\] Send failed seq \d+, error: (-?\d+)", timeout=2, since=m)
            bench.note(f"raw error {raw.group(1)}, text error {text.group(1)}")
            assert int(raw.group(1)) != 0 and int(text.group(1)) != 0
        finally:
            w.run("?DEBUG,OFF")
            _clear(w, "S2")


@test("map.raw_bypasses_etm", "Raw mapping traffic creates no ETM sequence, no ACK and no ?STATS counter change", needs=["wcb1"])
def raw_bypasses_etm(bench):
    s2 = link(bench, 1, "S2")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    data = padded("R", 32).encode()
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,R,W2S2")
            w.run("?DEBUG,ETM,ON")
            before = w.etm_board_stats().get(2)
            watch = Watch(w2s2)
            m = w.dev.mark()
            s2.send(data)
            watch.expect(w2s2, data, timeout=3)
            time.sleep(2.0)
            sent = [x for x in w.dev.since(m) if x.startswith("[ETM] Sent seq")]
            after = w.etm_board_stats().get(2)
            assert not sent, f"raw traffic created ETM sends: {sent}"
            assert before and after and before["sent"] == after["sent"] and before["ackd"] == after["ackd"], f"{before} -> {after}"
        finally:
            w.run("?DEBUG,ETM,OFF")
            _clear(w, "S2")


@test("map.raw_remote_soft_port", "Raw from a W1 soft input to a soft port on WCB2 arrives exactly (16 bytes)", needs=["wcb1"], links=["W1S3", "W2S3|W2S4|W2S5"])
def raw_remote_soft_port(bench):
    s3 = link(bench, 1, "S3")
    tokens = bench.config_tokens(2, refresh=True)
    busy = " ".join(t for t in tokens if t.upper().startswith(("?HCR", "?MP3", "?DFP", "?MAESTRO,M", "?WLED")))
    q = next((p for p in ("S3", "S4", "S5") if bench.links.usable(2, p) and f"W2{p}" not in busy), None)
    if not q:
        raise Skip("no wired W2 soft port without a device")
    dst = link(bench, 2, q)
    w = usb_wcb(bench)
    _no_mappings(w)
    data = b"HIL" + padded("", 16)[3:16].encode()
    with config_guard(bench, 1):
        try:
            assert _has(w.run(f"?MAP,SERIAL,S3,R,W2{q}"), "Serial mapping set: Serial3 (RAW) -> 1 destination(s)")
            watch = Watch(dst)
            s3.send(data)
            watch.expect(dst, data, timeout=3)
            errs = dst.errors(watch.marks[dst.key])
            assert not errs, f"framing errors on W2 {q}: {errs}"
        finally:
            _clear(w, "S3")
            w.run("?BAUD,S3,9600")   # re-evaluate S3's soft-TX protection (applied only at begin, WCB.ino:2042-2066)


# ============================================================ persistence and restore fidelity
@test("map.persistence_reboot", "Mappings, raw flags and the auto-set broadcast flags survive a reboot (two W1 reboots)", needs=["wcb1"])
def persistence_reboot(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,R,S4,W2S2")
            w.run("?MAP,SERIAL,S3,R,S5")
            assert _has(w.run("?MAP,SERIAL,S3,S5"), "Serial mapping set: Serial3 -> 1 destination(s)")
            m = w.reboot()
            boot = w.dev.since(m)
            assert any(re.search(r"Mapping \d+: Serial2 \(RAW\) -> S4, W2S2", x) for x in boot), "boot log lacks the S2 raw mapping"
            assert any(re.search(r"Mapping \d+: Serial3 -> S5$", x.rstrip()) for x in boot), "boot log lacks the S3 text mapping"
            n = marker()
            raw = b"\x01\x02" + n.encode()
            watch = Watch(s4, w2s2, s5)
            s2.send(raw)
            watch.expect(s4, raw, timeout=3)
            watch.expect(w2s2, raw, timeout=3)
            prime(s3)
            time.sleep(0.3)
            t = marker("t")
            s3.send(t.encode() + b"\r")
            watch.expect(s5, t.encode() + b"\r", timeout=3)
            tokens = snapshot(bench, 1)
            for want in ("?BCAST,OUT,S2,OFF", "?BCAST,IN,S2,OFF", "?BCAST,OUT,S3,OFF", "?BCAST,IN,S3,OFF",
                         "?MAP,SERIAL,S2,R,S4,W2S2", "?MAP,SERIAL,S3,S5"):
                assert want in tokens, f"?backup lacks {want} after reboot"
            _clear(w, "S2", "S3")
            m = w.reboot()
            assert any("No serial mappings configured" in x for x in w.dev.since(m)), "mappings came back after CLEAR + reboot"
        finally:
            _clear(w, "S2", "S3")


@test("map.clear_one_restores_flags", "(should) CLEAR,S<n> restores the port's broadcast flags to what they were before the mapping", needs=["wcb1"], links=[])
def clear_one_restores_flags(bench):
    """Restore-fidelity bug: removeSerialMonitorMapping forces broadcast IN and OUT back ON whatever they were
    (WCB_Storage.cpp:1970-1982); the mapping never remembers the port's previous flags."""
    w = usb_wcb(bench)
    _no_mappings(w)
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,IN,S2,ON")
    with config_guard(bench, 1):
        try:
            w.run("?BCAST,OUT,S2,OFF")
            w.run("?BCAST,IN,S2,OFF")
            w.run("?MAP,SERIAL,S2,S4")
            w.run("?MAP,SERIAL,CLEAR,S2")
            tokens = snapshot(bench, 1)
            flags = [t for t in tokens if t.startswith(("?BCAST,OUT,S2,", "?BCAST,IN,S2,"))]
            assert "?BCAST,OUT,S2,OFF" in tokens and "?BCAST,IN,S2,OFF" in tokens, \
                f"clearing the mapping changed S2's deliberately-OFF broadcast flags: {flags}"
        finally:
            _clear(w, "S2")
            w.run("?BCAST,OUT,S2,ON")
            w.run("?BCAST,IN,S2,ON")


@test("map.clear_all_restores_output", "(should) ?MAP,SERIAL,CLEAR,ALL re-enables broadcast output on formerly mapped ports", needs=["wcb1"], links=[])
def clear_all_restores_output(bench):
    """Probable firmware bug: clearAllSerialMonitorMappings resets input blocking but never restores
    serialBroadcastEnabled (WCB_Storage.cpp:1996-2016), unlike removeSerialMonitorMapping (1976-1982)."""
    w = usb_wcb(bench)
    _no_mappings(w)
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,IN,S2,ON")
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,S4")
            assert _has(w.run("?MAP,SERIAL,CLEAR,ALL"), "All serial mappings cleared")
            tokens = snapshot(bench, 1)
            assert "?BCAST,IN,S2,ON" in tokens, "input blocking stayed on"
            assert "?BCAST,OUT,S2,ON" in tokens, "CLEAR,ALL left broadcast OUTPUT disabled on S2"
        finally:
            _clear(w, "S2")
            w.run("?BCAST,OUT,S2,ON")
            w.run("?BCAST,IN,S2,ON")


@test("map.raw_text_toggle", "'S2R' sets raw mode, lowercase works, re-issuing toggles raw/text live, backup normalises", needs=["wcb1"])
def raw_text_toggle(bench):
    s2, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    a, b = marker("a"), marker("b")
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,SERIAL,S2R,S4"), "Serial mapping set: Serial2 (RAW) -> 1 destination(s)")
            assert "?MAP,SERIAL,S2,R,S4" in snapshot(bench, 1)
            assert _has(w.run("?map,serial,s2,s4"), "Serial mapping set: Serial2 -> 1 destination(s)")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s4, s5)
            s2.send(f";S5{a}\r".encode())
            watch.expect(s5, a.encode() + b"\r", timeout=2)
            watch.silent(s4, window=0.8)
            assert _has(w.run("?map,serial,s2,r,s4"), "Serial mapping set: Serial2 (RAW) -> 1 destination(s)")
            watch = Watch(s4, s5)
            s2.send(f";S5{b}\r".encode())
            watch.expect(s4, f";S5{b}\r".encode(), timeout=2)
            watch.silent(s5, window=0.8)
        finally:
            _clear(w, "S2")


@test("map.legacy_sm", "Legacy ?SMSx, ?SMLIST, ?SMRSx and ?SM reach the same functions", needs=["wcb1"], links=[])
def legacy_sm(bench):
    w = usb_wcb(bench)
    _no_mappings(w)
    with config_guard(bench, 1):
        try:
            checks = [
                ("?SMS2,S4", ["Serial mapping set: Serial2 -> 1 destination(s)"]),
                ("?SMLIST", ["Serial2 -> S4"]),
                ("?SMS2R,S4", ["Serial mapping set: Serial2 (RAW) -> 1 destination(s)"]),
                ("?SMLIST", ["Serial2 (RAW) -> S4"]),
                ("?SMRS2", ["Serial mapping removed for Serial2"]),
                ("?SMRS2", ["No mapping found for Serial2"]),
                ("?SM", ["Invalid format. Use: ?SMSx[R],dest1[R],dest2[R],..."]),
            ]
            bad = [f"{cmd!r} -> {out}" for cmd, wants in checks for out in [w.run(cmd)] if not all(_has(out, x) for x in wants)]
            assert not bad, "; ".join(bad)
        finally:
            _clear(w, "S2")


@test("map.baud_on_raw_mapped_port", "?BAUD on a raw-mapped soft port during traffic neither reboots nor wedges forwarding", needs=["wcb1"])
def baud_on_raw_mapped_port(bench):
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_mappings(w)
    end = marker("end")
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S3,R,S4")
            stop = threading.Event()

            def stream():
                while not stop.is_set():
                    s3.send(padded("Z", 32).encode())
                    time.sleep(0.04)

            m = w.dev.mark()
            th = threading.Thread(target=stream, daemon=True)
            th.start()
            outs = []
            for _ in range(5):
                outs.append(w.run("?BAUD,S3,9600"))
                time.sleep(0.3)
            stop.set()
            th.join()
            assert all(_has(o, "Baud rate for Serial3 updated to 9600") for o in outs), outs
            assert not any("Raw Serial Forwarding Task Created" in x for x in w.dev.since(m)), "WCB1 rebooted"
            time.sleep(0.5)
            watch = Watch(s4)
            s3.send(end.encode())
            watch.expect(s4, end.encode(), timeout=3)
        finally:
            _clear(w, "S3")
            w.run("?BAUD,S3,9600")


@test("map.input_port_loses_output", "While mapped, the input port receives no plain broadcasts", needs=["wcb1"])
def input_port_loses_output(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    t = marker()
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,S4")
            watch = Watch(s2, s3, s4, s5)
            m = w.dev.mark()
            w.send(t)
            for l in (s3, s4, s5):
                watch.expect(l, t.encode() + b"\r", timeout=2)
            watch.silent(s2, window=1.5)
            assert not any("Broadcast blocked from Serial" in x for x in w.dev.since(m))
        finally:
            _clear(w, "S2")


@test("map.timer_line_obeys_mapping", "(should) A line containing ;T on a mapped port still goes only to the mapping", needs=["wcb1"])
def timer_line_obeys_mapping(bench):
    """Probable firmware bug: an un-prefixed line with ';T' goes to parseCommandGroups without the source port
    (WCB.ino:7163-7166) and every group is re-enqueued with sourceID 0 (command_timer.cpp:237), so it skips the
    mapping and broadcasts mesh-wide. The wiki says a mapping overrides broadcast (Broadcast.md:269)."""
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_mappings(w)
    x, y = marker("x"), marker("y")
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,S4")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s3, s4, s5)
            s2.send(f"{x}^;T300,{y}\r".encode())
            time.sleep(2.0)
            leaked = [l.key for l in (s3, s5) if x.encode() in watch.got(l) or y.encode() in watch.got(l)]
            delivered = x.encode() in watch.got(s4) and y.encode() in watch.got(s4)
            assert delivered and not leaked, f"mapped destination S4 got both markers: {delivered}; leaked to {leaked}"
        finally:
            _clear(w, "S2")


@test("map.remote_leading_comma", "(should) A mapped line starting with ',' reaches a remote destination intact, like a local one", needs=["wcb1"])
def remote_leading_comma(bench):
    """Probable firmware bug: a remote text destination is built as ';S<p>' + line (WCB.ino:6922) and the receiver
    strips one ',' after the port digit (WCB.ino:6487-6492), so a CSV-style line loses its first character remotely."""
    s2, s4 = link(bench, 1, "S2"), link(bench, 1, "S4")
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    _no_mappings(w)
    line = f",{marker()}"
    with config_guard(bench, 1):
        try:
            w.run("?MAP,SERIAL,S2,S4,W2S2")
            prime(s2)
            time.sleep(0.3)
            watch = Watch(s4, w2s2)
            s2.send(line.encode() + b"\r")
            watch.expect(s4, line.encode() + b"\r", timeout=2)
            time.sleep(1.5)
            assert line.encode() + b"\r" in watch.got(w2s2), f"remote got {watch.got(w2s2)!r}, local got {watch.got(s4)!r}"
        finally:
            _clear(w, "S2")
