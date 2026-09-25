"""?WLED configuration: local and remote slots, LIST / STATUS / CLEAR, validation, the port reservation, the `;L`
route to a local port, the legacy ?WLED,PORT form, and WDP auto-add of a remote proxy on W2.

s09 covers the `;L` bytes through W1's proxy for W2's WLED 1; this suite covers the configuration itself
(WCB_WLED.cpp configureWLED / clearWLEDByID / clearWLEDConfig / printWLEDSettings / printWLEDStatus / wledAutoAddRemote).
Rules:
- A local WLED is advertised over WDP and every other board auto-adds a never-evicted proxy for it
  (wledAutoAddRemote, first-host-wins), so a local add runs with W1's WDP off - except wled.wdp_auto_add, which is
  about that and clears W2's proxy itself.
- Reserving a port sets its baud, turns both broadcast flags off and labels it "WLED <id>"; releasing it resets 9600,
  ON/ON and an empty label (wledReserveLocalPort / wledReleaseLocalPort). So only ports whose baseline is exactly
  that are used, and the baseline label is re-issued afterwards.
- One slot per id: re-configuring an id moves it (local <-> remote), so W1's proxy for WLED 1 is restored from its
  own chain token after the legacy ?WLED,PORT form re-homed it.
"""
import re
import time
from contextlib import contextmanager

from hil.runner import Skip, test
from suites.common import Console, Watch, config_guard, link, marker, require_tokens, snapshot, token, usb_wcb

ON = b'{"on":true}\n'


def _has(lines, text):
    return any(text in x for x in lines)


def _in_order(lines, wanted, label):
    i = 0
    for n in wanted:
        while i < len(lines) and n not in lines[i]:
            i += 1
        if i == len(lines):
            return [f"{label}: missing '{n}' (in order) - got {lines}"]
        i += 1
    return []


def _wled_tokens(tokens):
    return [t for t in tokens if t.upper().startswith("?WLED,")]


def _ids_free(bench, *ids):
    used = {int(m.group(1)) for w in (1, 2) for t in _wled_tokens(bench.config_tokens(w, refresh=True))
            for m in [re.match(r"^\?WLED,(\d+):", t, re.I)] if m}
    clash = sorted(set(ids) & used)
    if clash:
        raise Skip(f"WLED id(s) {clash} already configured on W1 or W2")


def _require_free(bench, wcb, port):
    """Skip unless W<wcb> <port> is what a WLED release restores it to (9600, ON/ON) and no device or mapping owns it."""
    require_tokens(bench, wcb, f"?BAUD,{port},9600", f"?BCAST,OUT,{port},ON", f"?BCAST,IN,{port},ON")
    busy = [t for t in bench.config_tokens(wcb)
            if re.search(rf"(?:\b|W{wcb}){port}\b", t) and not t.upper().startswith(("?BAUD,", "?BCAST,", "?LABEL,", "?SEQ,"))]
    if busy:
        raise Skip(f"W{wcb} {port} is in use: {busy}")


def _relabel(w, before, *ports):
    """A release clears the port's label: put the baseline's back."""
    for p in ports:
        orig = token(before, f"?LABEL,{p},")
        w.run(orig if orig else f"?LABEL,CLEAR,{p}")


@contextmanager
def _wdp_off(w, tokens):
    if "?WDP,OFF" in tokens:
        raise Skip("W1's WDP is already off, so this test could not tell its own ?WDP,OFF apart from the bench's")
    if not _has(w.run("?WDP,OFF"), "[WDP] disabled"):
        raise AssertionError("?WDP,OFF did not confirm")
    try:
        yield
    finally:
        w.run("?WDP,ON")


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


# ============================================================ read-only
@test("wled.list_status_formats", "?WLED,LIST and ?WLED,STATUS on W1 and W2 list exactly the slots their config chains hold, local and remote", needs=["wcb1"], links=[])
def list_status_formats(bench):
    w = usb_wcb(bench)
    bad = []
    for wcb in (1, 2):
        toks = _wled_tokens(snapshot(bench, wcb))
        want_list, want_status = [], []
        for t in toks:
            m = re.match(rf"^\?WLED,(\d+):W(\d+)S(\d):(\d+)$", t, re.I)
            if not m:
                bad.append(f"W{wcb}: unexpected chain token {t}")
                continue
            wid, host, port, baud = m.groups()
            if int(host) == wcb:
                want_list.append(f"  WLED {wid} : local S{port} @ {baud} baud")
                want_status.append(f"[WLED:id={wid},port={port},baud={baud}]")
            else:
                want_list.append(f"  WLED {wid} : remote on WCB{host} @ {baud} baud")
                want_status.append(f"[WLED:id={wid},wcb={host},baud={baud}]")
        if wcb == 1:
            lst, st = [x.rstrip() for x in w.run("?WLED,LIST")], [x.rstrip() for x in w.run("?WLED,STATUS")]
        else:
            with Console(bench, 2) as c2:
                lst, st = _crun(c2, "?WLED,LIST"), _crun(c2, "?WLED,STATUS")
        bad += _in_order(lst, ["---- WLED Configuration ----"] + (want_list or ["  Not configured."]), f"W{wcb} LIST")
        rows = [x for x in st if x.startswith("[WLED:id=")]
        if f"[WLED:cnt={len(toks)}]" not in st or rows != want_status:
            bad.append(f"W{wcb} STATUS {st}, expected cnt={len(toks)} and {want_status}")
    assert not bad, "; ".join(bad)


@test("wled.config_rejects", "?WLED refusals on W1 (id, destination, baud, port, soft-serial baud block, a reserved port, WCB range, CLEAR forms) and ;L to a missing id change nothing", needs=["wcb1"], links=[])
def config_rejects(bench):
    """configureWLED, clearWLEDByID and processWLEDRuntimeCommand (WCB_WLED.cpp). The soft-serial block refuses 115200
    on S3-S5, which receive exactly through 57600 only (CLAUDE.md rule 13; it refused anything above 9600 until
    2026-09-24, docs/HIL_TEST_AUDIT.md F1). S1 is refused because Maestro_Remote reserves it (kyberModeReservesPort)."""
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?MAESTRO,REMOTE")
    _ids_free(bench, 5)
    me = bench.usb_wcb_number()
    usage = f"[WLED] Usage: ?WLED,<id>:W<wcb>S<port>:<baud>  (e.g. 1:W{me}S1:115200)"
    checks = [
        ("?WLED,0:W1S2:115200", ["[WLED] Invalid WLED ID. Must be 1-9"]),
        ("?WLED,10:W1S2:115200", ["[WLED] Invalid WLED ID. Must be 1-9"]),
        ("?WLED,5", [usage]),
        ("?WLED,5:W1S2", ["[WLED] Missing baud. Use ...S<port>:<baud>"]),
        ("?WLED,5:X2:115200", ["[WLED] Destination must be W<wcb>S<port> (e.g. W2S1)"]),
        ("?WLED,5:W1S2:4800", ["[WLED] Baud must be 9600/19200/38400/57600/115200 (WLED default 115200)"]),
        ("?WLED,5:W21S1:115200", ["[WLED] Invalid WCB number. Must be W1-W20"]),
        ("?WLED,5:W1S6:115200", ["[WLED] Invalid serial port. Must be S1-S5"]),
        ("?WLED,5:W1S4:115200", ["S4 is SOFTWARE SERIAL — 115200 baud is not received reliably (measured exact through 57600)",
                                 "A WLED defaults to 115200 — use a HARDWARE port (S1/S2), or set the WLED to 57600 for this port.",
                                 "❌ CONFIGURATION BLOCKED!", "  Use: ?WLED,5:W1S1:115200   or   S2:115200   or   S4:57600"]),
        ("?WLED,5:W1S1:115200", ["[WLED] S1 already in use by PWM/Kyber/MP3/HCR/DFP - config blocked"]),
        ("?WLED,CLEAR,0", ["[WLED] Usage: ?WLED,CLEAR,<id 1-9>"]),
        ("?WLED,CLEAR,5", ["[WLED] WLED 5 not configured"]),
        ("?WLED,PORT,X", ["[WLED] Legacy usage: ?WLED,PORT,S<port>:<baud>"]),
        (";L5,ON", ["[WLED] WLED 5 not configured"]),
    ]
    if not any(re.match(rf"^\?WLED,\d+:W{me}S", t, re.I) for t in bench.config_tokens(1)):
        checks.append((";L,ON", [f"[WLED] No local WLED — use ?WLED,<id>:W{me}S<port>:115200"]))
    with config_guard(bench, 1):
        bad = [f"{cmd!r} -> {out}" for cmd, wants in checks for out in [w.run(cmd)] if not all(_has(out, x) for x in wants)]
        if _has(w.run("?WLED,STATUS"), "[WLED:id=5,"):
            bad.append("a refused ?WLED created WLED 5")
    assert not bad, "; ".join(bad)


# ============================================================ local slots
@test("wled.local_add_move_clear", "A local WLED on W1 S4 reserves the port (9600, flags off, label), takes ;L bytes, shares the port with a second id, moves to S5 releasing S4, and CLEAR releases S5 (WDP off)", needs=["wcb1"])
def local_add_move_clear(bench):
    s4, s5 = link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _require_free(bench, 1, "S4")
    _require_free(bench, 1, "S5")
    _ids_free(bench, 3, 4)
    bad = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            try:
                out = [x.rstrip() for x in w.run(f"?WLED,3:W{me}S4:9600")]
                bad += _in_order(out, ["  ⚠️  Disabled broadcast output on S4 (WLED port)", "  ⚠️  Disabled broadcast input on S4 (WLED port)",
                                       "Serial4 label set to: 'WLED 3'", "[WLED] WLED 3: local S4 at 9600 baud (slot "], "add 3 on S4")
                if _has(out, "Baud rate for Serial4"):
                    bad.append("the baud was re-applied although S4 was already at 9600")
                toks = snapshot(bench, 1)
                bad += [f"chain lacks {t}" for t in (f"?WLED,3:W{me}S4:9600", "?BCAST,OUT,S4,OFF", "?BCAST,IN,S4,OFF", "?LABEL,S4,WLED 3") if t not in toks]
                watch = Watch(s4, s5)
                w.send(";L3,ON")
                try:
                    watch.expect(s4, ON, timeout=2)
                except AssertionError:
                    bad.append(f";L3,ON did not reach W1 S4: {watch.got(s4)!r}")
                watch = Watch(s4, s5)
                w.send(";L,ON")                      # bare ;L: the lowest local id
                try:
                    watch.expect(s4, ON, timeout=2)
                except AssertionError:
                    bad.append(f"bare ;L,ON did not reach W1 S4: {watch.got(s4)!r}")
                time.sleep(0.5)
                if watch.got(s5):
                    bad.append(f"a ;L for WLED 3 reached S5: {watch.got(s5)!r}")
                t = marker()
                watch = Watch(s4)
                w.send(t)                             # a broadcast must skip the WLED port (its output flag is off)
                time.sleep(1.5)
                if t.encode() in watch.got(s4):
                    bad.append("a broadcast reached the WLED port")
                # a second id on the same port: no second reservation, and clearing 3 keeps the port for 4
                out = [x.rstrip() for x in w.run(f"?WLED,4:W{me}S4:9600")]
                if not _has(out, "[WLED] WLED 4: local S4 at 9600 baud (slot ") or _has(out, "Disabled broadcast"):
                    bad.append(f"a second id on S4: {out}")
                out = [x.rstrip() for x in w.run("?WLED,CLEAR,3")]
                if not _has(out, "[WLED] WLED 3 cleared") or _has(out, "Released S4"):
                    bad.append(f"clearing 3 while 4 shares S4: {out}")
                if "?BCAST,OUT,S4,OFF" not in snapshot(bench, 1):
                    bad.append("S4 was released although WLED 4 still uses it")
                # move 4 to S5: S4 released (9600, ON/ON, no label), S5 reserved
                out = [x.rstrip() for x in w.run(f"?WLED,4:W{me}S5:9600")]
                bad += _in_order(out, ["  ⚠️  Disabled broadcast output on S5 (WLED port)", "Serial5 label set to: 'WLED 4'",
                                       "Baud rate for Serial4 updated to 9600", "Serial4 label set to: ''", "  ✓ Released S4 (old WLED port)",
                                       "[WLED] WLED 4: local S5 at 9600 baud (slot "], "move 4 to S5")
                toks = snapshot(bench, 1)
                bad += [f"after the move the chain lacks {t}" for t in ("?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON", f"?WLED,4:W{me}S5:9600",
                                                                        "?BCAST,OUT,S5,OFF", "?LABEL,S5,WLED 4") if t not in toks]
                if [t for t in toks if t.upper().startswith("?LABEL,S4,")]:
                    bad.append("S4 kept a label after the move")
                watch = Watch(s4, s5)
                w.send(";L4,OFF")
                try:
                    watch.expect(s5, b'{"on":false}\n', timeout=2)
                except AssertionError:
                    bad.append(f";L4,OFF did not reach W1 S5: {watch.got(s5)!r}")
                time.sleep(0.5)
                if watch.got(s4):
                    bad.append(f"the old port S4 still received WLED bytes: {watch.got(s4)!r}")
                lst = [x.rstrip() for x in w.run("?WLED,LIST")]
                if "  WLED 4 : local S5 @ 9600 baud" not in lst or _has(lst, "WLED 3 "):
                    bad.append(f"LIST after the move: {lst}")
                out = [x.rstrip() for x in w.run("?WLED,CLEAR,4")]
                bad += _in_order(out, ["Baud rate for Serial5 updated to 9600", "Serial5 label set to: ''", "  ✓ Released S5 (old WLED port)",
                                       "[WLED] WLED 4 cleared"], "clear 4")
            finally:
                for wid in (3, 4):
                    w.run(f"?WLED,CLEAR,{wid}")     # "not configured" when already gone
                _relabel(w, before[1], "S4", "S5")
    assert not bad, "; ".join(bad)


@test("wled.legacy_port_form", "Legacy ?WLED,PORT,S<port>:<baud> is WLED 1 local on that port; it re-homes W1's proxy for WLED 1, which its chain token restores (WDP off)", needs=["wcb1"])
def legacy_port_form(bench):
    s4 = link(bench, 1, "S4")
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _require_free(bench, 1, "S4")
    bad = []
    with config_guard(bench, 1) as before:
        proxy = next((t for t in _wled_tokens(before[1]) if re.match(r"^\?WLED,1:W(\d+)S", t, re.I) and not re.match(rf"^\?WLED,1:W{me}S", t, re.I)), None)
        if any(re.match(rf"^\?WLED,1:W{me}S", t, re.I) for t in before[1]):
            raise Skip("W1 already hosts WLED 1")
        with _wdp_off(w, before[1]):
            try:
                out = [x.rstrip() for x in w.run("?WLED,PORT,S4:9600")]
                bad += _in_order(out, ["Serial4 label set to: 'WLED 1'", "[WLED] WLED 1: local S4 at 9600 baud (slot "], "?WLED,PORT")
                toks = snapshot(bench, 1)
                if f"?WLED,1:W{me}S4:9600" not in toks or (proxy and proxy in toks):
                    bad.append(f"chain after ?WLED,PORT: {_wled_tokens(toks)}")
                watch = Watch(s4)
                w.send(";L1,ON")
                try:
                    watch.expect(s4, ON, timeout=2)
                except AssertionError:
                    bad.append(f";L1,ON did not reach W1 S4: {watch.got(s4)!r}")
            finally:
                if proxy:
                    out = [x.rstrip() for x in w.run(proxy)]     # remote again: releases S4
                    if not _has(out, "[WLED] WLED 1: remote on WCB") or not _has(out, "Released S4"):
                        bad.append(f"re-homing WLED 1 to its host printed {out}")
                else:
                    w.run("?WLED,CLEAR,1")
                _relabel(w, before[1], "S4")
    assert not bad, "; ".join(bad)


# ============================================================ remote slots
@test("wled.remote_proxy_route", "A remote proxy on W1 forwards ;L<id> to its host as an ETM unicast; the host without that id says so and writes nothing; CLEAR,<id> and CLEAR (all, the proxy for WLED 1 included) restore from the chain", needs=["wcb1"], links=[])
def remote_proxy_route(bench):
    w = usb_wcb(bench)
    _ids_free(bench, 7)
    w2s2 = bench.links.get(2, "S2")
    bad = []
    with config_guard(bench, 1) as before, Console(bench, 2) as c2:
        proxies = _wled_tokens(before[1])
        try:
            out = [x.rstrip() for x in w.run("?WLED,7:W2S1:115200")]
            if not _has(out, "[WLED] WLED 7: remote on WCB2 (slot "):
                bad.append(f"proxy add printed {out}")
            if "?WLED,7:W2S0:115200" not in snapshot(bench, 1):
                bad.append("the chain does not hold the proxy as W2S0")
            w.run("?DEBUG,ETM,ON")
            watch = Watch(w2s2)
            cm, wm = c2.mark(), w.dev.mark()
            w.send(";L7,ON")
            try:
                c2.expect(r"\[WLED\] WLED 7 not configured", timeout=4, since=cm)
            except AssertionError:
                bad.append("W2 did not report WLED 7 unconfigured")
            time.sleep(1.0)
            if not any(re.search(r"\[ETM\] Sent seq \d+: ;L7,ON", x) for x in w.dev.since(wm)):
                bad.append("the proxy did not forward ;L7,ON as an ETM unicast")
            if w2s2 and watch.got(w2s2):
                bad.append(f"bytes reached W2 S2 for an unconfigured id: {watch.got(w2s2)!r}")
            out = w.run("?WLED,CLEAR,7")
            if not _has(out, "[WLED] WLED 7 cleared"):
                bad.append(f"CLEAR,7 printed {out}")
            out = w.run("?WLED,CLEAR")
            if not _has(out, "[WLED] All WLED configuration cleared") or _wled_tokens(snapshot(bench, 1)):
                bad.append(f"?WLED,CLEAR: {out}")
            for t in proxies:                       # the chain restores every proxy, W2S0 form included
                out = w.run(t)
                if not _has(out, "remote on WCB"):
                    bad.append(f"re-adding {t} printed {out}")
        finally:
            w.run("?DEBUG,ETM,OFF")
            w.run("?WLED,CLEAR,7")
            have = _wled_tokens(snapshot(bench, 1))
            for t in proxies:
                if t not in have:
                    w.run(t)
    assert not bad, "; ".join(bad)


@test("wled.wdp_auto_add", "A local WLED on W1 (WDP on) makes W2 auto-add a remote proxy and refresh its baud; W1 clearing the WLED leaves W2's proxy (never evicted: characterisation), which the test clears", needs=["wcb1"])
def wdp_auto_add(bench):
    """wledAutoAddRemote (WCB_WLED.cpp): first-host-wins, idempotent, persisted on W2. W1 S2 is a hardware port, so the
    WLED default 115200 is allowed there; the wire is pinned to each rate."""
    s2 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _require_free(bench, 1, "S2")
    _ids_free(bench, 3)
    if "?WDP,OFF" in bench.config_tokens(1) or "?WDP,OFF" in bench.config_tokens(2):
        raise Skip("WDP is off on W1 or W2")
    bad = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        try:
            cm = c2.mark()
            out = w.run(f"?WLED,3:W{me}S2:115200")
            if not _has(out, "[WLED] WLED 3: local S2 at 115200 baud (slot "):
                raise AssertionError(f"local add printed {out}")
            try:
                c2.expect(rf"\[WDP\] auto-added remote WLED 3 on WCB{me} @ 115200 baud \(slot \d\)", timeout=6, since=cm)
            except AssertionError:
                w.run("?WDP,POLL")
                c2.expect(rf"\[WDP\] auto-added remote WLED 3 on WCB{me} @ 115200 baud \(slot \d\)", timeout=6, since=cm)
            time.sleep(1.0)
            if f"?WLED,3:W{me}S0:115200" not in snapshot(bench, 2):
                bad.append("W2's chain lacks the auto-added proxy")
            s2.listen(115200)
            watch = Watch(s2)
            w.send(";L3,ON")
            try:
                watch.expect(s2, ON, timeout=2)
            except AssertionError:
                bad.append(f";L3,ON did not reach W1 S2 at 115200: {watch.got(s2)!r}")
            cm = c2.mark()
            out = w.run(f"?WLED,3:W{me}S2:57600")
            if not _has(out, "[WLED] WLED 3: local S2 at 57600 baud (slot "):
                bad.append(f"baud change printed {out}")
            try:
                c2.expect(rf"\[WDP\] WLED 3 @ WCB{me} baud updated to 57600", timeout=6, since=cm)
            except AssertionError:
                w.run("?WDP,POLL")
                try:
                    c2.expect(rf"\[WDP\] WLED 3 @ WCB{me} baud updated to 57600", timeout=6, since=cm)
                except AssertionError:
                    bad.append("W2 did not refresh the proxy's baud from W1's advert")
            out = w.run("?WLED,CLEAR,3")
            if not _has(out, "[WLED] WLED 3 cleared"):
                bad.append(f"CLEAR,3 printed {out}")
            w.run("?WDP,POLL")
            time.sleep(3.0)
            kept = [t for t in snapshot(bench, 2) if t.upper().startswith("?WLED,3:")]
            bench.note(f"after W1 cleared WLED 3, W2 still holds {kept} (proxies are never evicted)")
            if not kept:
                bench.note("W2 dropped the proxy on its own - the never-evicted rule changed")
        finally:
            w.run("?WLED,CLEAR,3")
            _relabel(w, before[1], "S2")
            _crun(c2, "?WLED,CLEAR,3", 1.0)
            time.sleep(1.0)
    assert not bad, "; ".join(bad)


@test("wled.soft_port_57600", "A local WLED at 57600 on W1 S4 is accepted (soft ports take up to 57600 since 2026-09-24, F1): S4 goes to 57600 with its flags off and label, the chain holds it, ;L bytes arrive at 57600; CLEAR puts S4 back (WDP off)", needs=["wcb1"])
def soft_port_57600(bench):
    s4 = link(bench, 1, "S4")
    w = usb_wcb(bench)
    me = bench.usb_wcb_number()
    _require_free(bench, 1, "S4")
    _ids_free(bench, 5)
    bad = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            try:
                out = [x.rstrip() for x in w.run(f"?WLED,5:W{me}S4:57600")]
                bad += _in_order(out, ["  ⚠️  Disabled broadcast output on S4 (WLED port)", "  ⚠️  Disabled broadcast input on S4 (WLED port)",
                                       "Serial4 label set to: 'WLED 5'", "[WLED] WLED 5: local S4 at 57600 baud (slot "], "add 5 on S4 at 57600")
                if not _has(out, "Baud rate for Serial4 updated to 57600"):
                    bad.append("S4 was not set to 57600")
                if _has(out, "SOFTWARE SERIAL") or _has(out, "BLOCKED"):
                    bad.append("57600 on S4 was refused as software serial")
                toks = snapshot(bench, 1)
                bad += [f"chain lacks {t}" for t in (f"?WLED,5:W{me}S4:57600", "?BAUD,S4,57600", "?BCAST,OUT,S4,OFF", "?BCAST,IN,S4,OFF",
                                                     "?LABEL,S4,WLED 5") if t not in toks]
                s4.listen(57600)
                watch = Watch(s4)
                w.send(";L5,ON")
                try:
                    watch.expect(s4, ON, timeout=2)
                except AssertionError:
                    bad.append(f";L5,ON did not reach W1 S4 at 57600: {watch.got(s4)!r}")
            finally:
                out = [x.rstrip() for x in w.run("?WLED,CLEAR,5")]
                if not _has(out, "Baud rate for Serial4 updated to 9600"):
                    bad.append("CLEAR did not put S4 back to 9600")
                _relabel(w, before[1], "S4")
    assert not bad, "; ".join(bad)
