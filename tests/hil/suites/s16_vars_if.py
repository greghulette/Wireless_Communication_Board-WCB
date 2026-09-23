"""Stored variables (;V, ;VP, ?VAR, ;M!) and IF gating.

Built from the vars_seq_inventory specs; the console literals were re-checked against WCB_Variables.cpp and WCB.ino.
Rules from the specs:
- Volatile variables never reach ?backup or ?MGMT,PULL (WCB_Variables.cpp:479), so config_guard cannot see one leak:
  every test clears what it made.
- ?VAR,CLEAR,ALL — or clearing a variable named 'all' in any case — wipes every variable, persistent ones included
  (WCB_Variables.cpp:327-328). Only W1 is ever wiped, and its ?VAR,SET tokens are replayed afterwards.
- Every persistent write and every ?VAR,CLEAR rewrites the whole wcb_vars NVS blob (WCB_Variables.cpp:76-93), so the
  100-persistent-variable test is opt-in (bench.json "opt_in": ["nvs_wear"]).
- The three mesh-client specs (var.mesh_client_sets and the sequence fan-out ones) live with the probe mesh-mode tests.
"""
import re
import time

from hil.runner import Skip, test
from suites.common import Console, config_guard, link, marker, nonce, snapshot, usb_wcb


def _has(lines, text):
    return any(text in x for x in lines)


def _not_set(name):
    return f"[VAR] '{name}' is not set (reads as 0)"


def _get(w, name):
    return next((x.rstrip() for x in w.run(f"?VAR,GET,{name}") if x.startswith("[VAR]")), None)


def _clear(w, *names):
    for n in names:
        w.run(f"?VAR,CLEAR,{n}")


def _values(w, name, steps):
    """Run each (command, expected int or None) and GET after it; returns the mismatches."""
    bad = []
    for cmd, want in steps:
        w.run(cmd)
        if want is None:
            continue
        got = _get(w, name)
        if got != f"[VAR] {name} = {want}":
            bad.append(f"{cmd}: {got!r}, expected {want}")
    return bad


def _var_list(w):
    """The ?VAR,LIST block, from its header to the 'N/100 used' footer."""
    lines = [x.rstrip() for x in w.run("?VAR,LIST")]
    start = lines.index("---- Variables ----") if "---- Variables ----" in lines else len(lines)
    block = lines[start:]
    end = next((i for i, x in enumerate(block) if re.match(r"^  \d+/100 used$", x) or x == "  (none)"), len(block) - 1)
    return block[:end + 1]


def _persistent_tokens(bench, wcb=1):
    return [t for t in snapshot(bench, wcb) if t.upper().startswith("?VAR,SET,")]


# ============================================================ ;V and ?VAR
@test("var.v_set_get_list", ";V sets a volatile int silently; ?VAR,GET / LIST / CLEAR formats", needs=["wcb1"], links=[])
def v_set_get_list(bench):
    w = usb_wcb(bench)
    w.run("?DEBUG,OFF")
    _clear(w, "hilv")
    try:
        out = w.run(";V,hilv,5")
        assert not [x for x in out if x.startswith("[VAR]")], f";V echoed with debug off: {out}"
        assert _get(w, "hilv") == "[VAR] hilv = 5"
        lst = _var_list(w)
        assert lst[:1] == ["---- Variables ----"] and "  hilv = 5  [volatile]" in lst, lst
        assert re.match(r"^  \d+/100 used$", lst[-1]), f"LIST footer {lst[-1:]}"
        assert _has(w.run("?VAR,CLEAR,hilv"), "[VAR] Cleared 'hilv'")
        assert _get(w, "hilv") == _not_set("hilv")
    finally:
        _clear(w, "hilv")


@test("var.v_verbs", ";V true/false/TOGGLE/INC/DEC (any case) and integer-literal parsing", needs=["wcb1"], links=[])
def v_verbs(bench):
    w = usb_wcb(bench)
    _clear(w, "hilv")
    try:
        bad = _values(w, "hilv", [(";V,hilv,true", 1), (";V,hilv,FALSE", 0), (";V,hilv,True", 1), (";V,hilv,TOGGLE", 0),
                                  (";V,hilv,toggle", 1), (";V,hilv,7", None), (";V,hilv,TOGGLE", 0)])
        _clear(w, "hilv")
        bad += _values(w, "hilv", [(";V,hilv,TOGGLE", 1)])      # unset reads 0
        _clear(w, "hilv")
        bad += _values(w, "hilv", [(";V,hilv,INC", 1), (";V,hilv,INC,5", 6), (";V,hilv,DEC", 5), (";V,hilv,DEC,10", -5),
                                   (";V,hilv,inc,-3", -8), (";V,hilv,INC,abc", -8)])
        bad += _values(w, "hilv", [(";V,hilv,12abc", 12), (";V,hilv,abc", 0), (";V,hilv,-42", -42), (";V,hilv,+7", 7)])
    finally:
        _clear(w, "hilv")
    assert not bad, "; ".join(bad)


@test("var.v_int32_limits", ";V out-of-range literals saturate at int32; INC past INT32_MAX is recorded (undefined behaviour)", needs=["wcb1"], links=[])
def v_int32_limits(bench):
    w = usb_wcb(bench)
    _clear(w, "hilv")
    try:
        bad = _values(w, "hilv", [(";V,hilv,99999999999", 2147483647), (";V,hilv,-99999999999", -2147483648)])
        w.run(";V,hilv,2147483647")
        w.run(";V,hilv,INC")
        bench.note(f"INC past INT32_MAX gives {_get(w, 'hilv')!r} (signed overflow; wraps in practice)")
    finally:
        _clear(w, "hilv")
    assert not bad, "; ".join(bad)


@test("var.v_errors", ";V / ;VP malformed forms print their error (not debug-gated) and create nothing", needs=["wcb1"], links=[])
def v_errors(bench):
    w = usb_wcb(bench)
    w.run("?DEBUG,OFF")
    _clear(w, "hilv")
    usage = "[VAR] usage: ;V,<name>,<value>  (volatile)  |  ;VP,<name>,<value>  (persistent)"
    value = "<int|true|false|TOGGLE|INC[,n]|DEC[,n]>"

    def invalid(name):
        return f"[VAR] Invalid name '{name}' — 1-15 chars, letters/digits/underscore only"

    checks = [(";V", usage), (";Vhilv,5", usage), (";VPhilv,5", usage),
              (";V,hilv", f"[VAR] ;V needs a value: ;V,hilv,{value}"), (";V,hilv,", f"[VAR] ;V needs a value: ;V,hilv,{value}"),
              (";VP,hilv", f"[VAR] ;VP needs a value: ;VP,hilv,{value}"), (";V,,5", invalid("")),
              (";V,hil_abcdefgh1234,1", invalid("hil_abcdefgh1234")), (";V,hil-x,1", invalid("hil-x")), (";V,hil x,1", invalid("hil x"))]
    bad = [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
    if _get(w, "hilv") != _not_set("hilv"):
        bad.append("a malformed ;V created hilv")
    assert not bad, "; ".join(bad)


@test("var.name_rules", "Names: 15 chars and a leading digit are fine; names are case-sensitive; GET does not validate", needs=["wcb1"], links=[])
def name_rules(bench):
    w = usb_wcb(bench)
    names = ("hil_abcdefgh123", "9hil", "HilCase")
    _clear(w, *names)
    try:
        for n in names:
            w.run(f";V,{n},1")
        got = {n: _get(w, n) for n in ("hil_abcdefgh123", "9hil", "hilcase", "HilCase", "hil-x")}
    finally:
        _clear(w, *names)
    want = {"hil_abcdefgh123": "[VAR] hil_abcdefgh123 = 1", "9hil": "[VAR] 9hil = 1", "hilcase": _not_set("hilcase"),
            "HilCase": "[VAR] HilCase = 1", "hil-x": _not_set("hil-x")}
    assert got == want, f"got {got}"


@test("var.debug_echo", ";V/;VP echo their value only under ?DEBUG,ON, even when unchanged", needs=["wcb1"], links=[])
def debug_echo(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1):
        _clear(w, "hilv", "hilp")
        try:
            assert _has(w.run("?DEBUG,ON"), "Debugging enabled")
            for cmd, want in ((";V,hilv,5", "[VAR] hilv = 5  [volatile]"), (";V,hilv,5", "[VAR] hilv = 5  [volatile]"),
                              (";VP,hilp,2", "[VAR] hilp = 2  [persistent]"), (";V,hilp,3", "[VAR] hilp = 3  [persistent]")):
                out = w.run(cmd)
                if want not in [x.rstrip() for x in out]:
                    bad.append(f"{cmd}: {out}")
            assert _has(w.run("?DEBUG,OFF"), "Debugging disabled")
            out = w.run(";V,hilv,6")
            if [x for x in out if x.startswith("[VAR]")]:
                bad.append(f";V echoed after debug off: {out}")
        finally:
            w.run("?DEBUG,OFF")
            _clear(w, "hilv", "hilp")
    assert not bad, "; ".join(bad)


@test("var.persistence_rules", ";VP persists across a reboot; ;V never demotes; ;VP promotes; only persistent vars reach ?backup (1 reboot)", needs=["wcb1"], links=[])
def persistence_rules(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1):
        _clear(w, "hilp", "hilv", "hilq")
        try:
            for cmd in (";VP,hilp,5", ";V,hilp,7", ";V,hilp,INC", ";V,hilv,3", ";V,hilq,4"):
                w.run(cmd)
            a = snapshot(bench, 1)
            if "?VAR,SET,hilp,8" not in a or [t for t in a if t.startswith(("?VAR,SET,hilv,", "?VAR,SET,hilq,"))]:
                bad.append(f"backup A variables {[t for t in a if t.startswith('?VAR,')]}")
            w.run(";VP,hilq,INC")
            lst = _var_list(w)
            bad += [f"LIST lacks {x!r}" for x in ("  hilp = 8  [persistent]", "  hilv = 3  [volatile]", "  hilq = 5  [persistent]") if x not in lst]
            b = snapshot(bench, 1)
            bad += [f"backup B lacks {t}" for t in ("?VAR,SET,hilp,8", "?VAR,SET,hilq,5") if t not in b]
            m = w.reboot()
            if not any(re.match(r"^\[VAR\] Loaded \d+ variable\(s\)$", x) for x in w.dev.since(m)):
                bad.append("the boot log has no '[VAR] Loaded N variable(s)' line")
            got = [_get(w, n) for n in ("hilp", "hilq", "hilv")]
            if got != ["[VAR] hilp = 8", "[VAR] hilq = 5", _not_set("hilv")]:
                bad.append(f"after reboot {got}")
        finally:
            _clear(w, "hilp", "hilq", "hilv")
    assert not bad, "; ".join(bad)


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _cget(console, name):
    return next((x for x in _crun(console, f"?VAR,GET,{name}") if x.startswith("[VAR]")), None)


@test("var.backup_emit_position", "?backup emits persistent vars as ?VAR,SET before ?ETM in the configured chain and in the factory chain; never volatile ones", needs=["wcb1"], links=[])
def backup_emit_position(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1):
        _clear(w, "hilp", "hilv")
        try:
            w.run("?VAR,SET,hilp,-12")
            w.run(";V,hilv,1")
            lines = [x.strip() for x in w.run("?backup", timeout=8)]
            tokens, _, _ = w.backup_chain()
            etm = next((i for i, t in enumerate(tokens) if t.startswith("?ETM,")), None)
            if "?VAR,SET,hilp,-12" not in tokens:
                bad.append("the configured chain lacks ?VAR,SET,hilp,-12")
            elif etm is not None and tokens.index("?VAR,SET,hilp,-12") > etm:
                bad.append("?VAR,SET comes after ?ETM in the configured chain")
            fac = next((i for i, x in enumerate(lines) if "*** === For Factory Reset/Fresh Boards" in x), None)
            chain = next((x for x in lines[fac + 1:] if "^" in x), "") if fac is not None else ""
            if "?VAR,SET,hilp,-12" not in chain:
                bad.append("the factory chain lacks ?VAR,SET,hilp,-12")
            pv = next((i for i, x in enumerate(lines) if x == "?VAR,SET,hilp,-12"), None)
            pe = next((i for i, x in enumerate(lines) if x.startswith("?ETM,")), None)
            if pv is not None and pe is not None and pv > pe:
                bad.append("the printed ?VAR,SET line comes after the first printed ?ETM line")
            if _has(lines, "hilv"):
                bad.append("the volatile hilv reached ?backup")
        finally:
            _clear(w, "hilp", "hilv")
    assert not bad, "; ".join(bad)


@test("var.config_formats", "?VAR SET/GET/CLEAR/LIST literals, usage errors, prefix-matched verbs, SET turning TOGGLE/INC into 0", needs=["wcb1"], links=[])
def config_formats(bench):
    w = usb_wcb(bench)
    invalid = "[VAR] Invalid name '{}' — 1-15 chars, letters/digits/underscore only"
    checks = [("?VAR,SET,hilp,42", "[VAR] hilp = 42  [persistent]"), ("?VAR,SET,hilp,42", "[VAR] hilp = 42  [persistent]"),
              ("?VAR,SET,hilp,true", "[VAR] hilp = 1  [persistent]"), ("?VAR,SET,hilp,TOGGLE", "[VAR] hilp = 0  [persistent]"),
              ("?VAR,SET,hilp,INC,2", "[VAR] hilp = 0  [persistent]"), ("?VAR,SET,hilp", "[VAR] Usage: ?VAR,SET,<name>,<value>"),
              ("?VAR,SET,bad-n,1", invalid.format("bad-n")), ("?VAR,SET", invalid.format("")),
              ("?VAR,GET", "[VAR] Usage: ?VAR,GET,<name>"), ("?VAR,CLEAR", "[VAR] Usage: ?VAR,CLEAR,<name|ALL>"),
              ("?VAR,SETUP,hilp,3", "[VAR] hilp = 3  [persistent]"), ("?VAR,GETX,hilp", "[VAR] hilp = 3"),
              ("?VAR,CLEARX,hilp", "[VAR] Cleared 'hilp'"), ("?VAR,CLEAR,hilp", "[VAR] 'hilp' not found"),
              ("?VAR,BOGUS", "[VAR] Unknown. Use: ?VAR,LIST | SET,<name>,<value> | GET,<name> | CLEAR,<name|ALL>"),
              ("?VAR", "---- Variables ----"), ("?var,list", "---- Variables ----")]
    with config_guard(bench, 1):
        _clear(w, "hilp")
        try:
            bad = [f"{cmd}: {out}" for cmd, want in checks for out in [w.run(cmd)] if want not in [x.rstrip() for x in out]]
        finally:
            _clear(w, "hilp")
    assert not bad, "; ".join(bad)


@test("var.help_trap", "A ?VAR command ending in '?' prints help and does not execute", needs=["wcb1"], links=[])
def help_trap(bench):
    w = usb_wcb(bench)
    _clear(w, "hilq")
    try:
        out = w.run("?VAR,SET,hilq,5?")
        assert _has(out, "  Wireless Communication Board (WCB) - Command Reference"), f"no top help menu: {out[:5]}"
        assert not [x for x in out if x.startswith("[VAR]")], "the command ran as well as printing help"
        assert _get(w, "hilq") == _not_set("hilq")
        assert _has(w.run("?VAR?"), "  ?VAR,LIST             List all variables and values")
    finally:
        _clear(w, "hilq")


@test("var.clear_all_name_collision", "(should) A variable named 'all' can be cleared on its own; ?VAR,CLEAR,all does not wipe every variable", needs=["wcb1"], links=[])
def clear_all_name_collision(bench):
    """Name collision: 'all' (any case) is a valid name (WCB_Variables.cpp:63-73), but ?VAR,CLEAR,<that name> clears the
    whole table, persistent variables included (WCB_Variables.cpp:327-328). W1's ?VAR,SET tokens are replayed after."""
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        saved = _persistent_tokens(bench, 1)
        w.run("?VAR,CLEAR,hila")
        try:
            w.run(";V,hila,1")
            w.run(";V,all,1")
            named = _get(w, "all")
            out = [x.rstrip() for x in w.run("?VAR,CLEAR,all")]
            survivor = _get(w, "hila")
        finally:
            w.run("?VAR,CLEAR,hila")
            if _get(w, "all") != _not_set("all"):      # only reachable on firmware that clears just the one variable
                w.run("?VAR,CLEAR,all")
            now = _persistent_tokens(bench, 1)
            if [t for t in saved if t not in now]:
                for t in saved:
                    w.run(t)
    bench.note(f"'all': GET {named!r}, CLEAR printed {out}, hila afterwards {survivor!r}")
    assert named != "[VAR] all = 1" or survivor == "[VAR] hila = 1", f"?VAR,CLEAR,all wiped every variable: {out}"


@test("var.cap_volatile_evicts", "At 100 variables a new ;V recycles the lowest-slot volatile one instead of failing (1 reboot)", needs=["wcb1"], links=[])
def cap_volatile_evicts(bench):
    """Code over doc: docs/VARIABLES_DESIGN.md:16 says set #101 is an error, but WCB_Variables.cpp:140-145 recycles the
    lowest volatile slot and errors only when all 100 are persistent. Restored by rebooting: 100 ?VAR,CLEARs would each
    rewrite the NVS blob."""
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        try:
            lst = _var_list(w)
            used = int(re.match(r"^  (\d+)/100", lst[-1]).group(1)) if lst and lst[-1].endswith(" used") else 0
            if used >= 100:
                raise Skip("W1 already holds 100 variables")
            w.send("^".join(f";V,hc{i},{i}" for i in range(100 - used)))   # ~1.1 KB: UART0 RX holds 8 KB (WCB.ino:7818)
            time.sleep(3)
            lst = _var_list(w)
            first = next((re.match(r"^  (\S+) = ", x).group(1) for x in lst if x.endswith("[volatile]")), None)
            quiet = [x for x in w.run(";V,hcnew,1") if x.startswith("[VAR]")]
            new, evicted, after = _get(w, "hcnew"), (_get(w, first) if first else None), _var_list(w)
        finally:
            w.reboot()
    assert lst[-1] == "  100/100 used", f"setup did not fill the table: {lst[-1:]}"
    assert not quiet, f";V at the cap printed {quiet}"
    assert new == "[VAR] hcnew = 1", f"hcnew: {new!r}"
    assert evicted == _not_set(first), f"the lowest volatile slot {first!r} survived: {evicted!r}"
    assert after[-1] == "  100/100 used", f"LIST footer {after[-1:]}"


@test("var.cap_persistent_full", "OPT-IN (nvs_wear): with 100 persistent variables, ;VP and ?VAR,SET print 'table full'", needs=["wcb1"], links=[], opt_in="nvs_wear")
def cap_persistent_full(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1):
        saved = _persistent_tokens(bench, 1)
        p = sum(1 for x in _var_list(w) if x.endswith("[persistent]"))
        try:
            for i in range(100 - p):
                if _has(w.run(f";VP,hp{i},{i}"), "NVS write returned 0 bytes"):
                    bad.append(f"NVS write failed at hp{i}")
                    break
            lst = _var_list(w)
            if lst[-1] != "  100/100 used" or [x for x in lst if x.endswith("[volatile]")]:
                bad.append(f"table not full of persistent vars: {lst[-3:]}")
            if not _has(w.run(";VP,hpX,1"), "[VAR] table full (100 persistent vars) — cannot create 'hpX'"):
                bad.append(";VP at the cap did not report table full")
            out = w.run("?VAR,SET,hpY,1")
            if not _has(out, "[VAR] table full (100 persistent vars) — cannot create 'hpY'") or _has(out, "[VAR] hpY = 1"):
                bad.append(f"?VAR,SET at the cap printed {out}")
        finally:
            w.run("?VAR,CLEAR,ALL")          # one blob write instead of a hundred
            for t in saved:
                w.run(t)
    assert not bad, "; ".join(bad)


@test("var.m_bang", ";M!<var>=<v> sets RAM-only vars only for names m1..m8*, and never overwrites a persistent var", needs=["wcb1"], links=[])
def m_bang(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 1):
        _clear(w, "m7hil", "m6hil")
        try:
            bad += _values(w, "m7hil", [(";M!m7hil=42", 42), (";m!m7hil=-3", -3)])
            if "  m7hil = -3  [volatile]" not in _var_list(w):
                bad.append("LIST lacks m7hil as volatile")
            for cmd in (";M!hilx=5", ";M!m9x=5", ";M!M1x=5", ";M!m1x", ";M!=5"):
                printed = [x for x in w.run(cmd) if x.startswith(("[VAR]", "[MAESTRO]"))]
                if printed:
                    bad.append(f"{cmd} printed {printed}")
            bad += [f"{n} was set" for n in ("hilx", "m9x", "M1x", "m1x") if _get(w, n) != _not_set(n)]
            w.run(";VP,m6hil,5")
            w.run(";M!m6hil=9")
            if "  m6hil = 5  [persistent]" not in _var_list(w):
                bad.append(";M! overwrote the persistent m6hil")
            tokens = snapshot(bench, 1)
            if "?VAR,SET,m6hil,5" not in tokens or _has(tokens, "m7hil"):
                bad.append("backup variables wrong")
            assert _has(w.run("?DEBUG,MAESTRO,ON"), "Maestro debugging enabled")
            if not _has(w.run(";M!m7hil=11"), "[MAESTRO] result m7hil=11 (from mesh)"):
                bad.append("no [MAESTRO] result line under debug")
        finally:
            w.run("?DEBUG,MAESTRO,OFF")
            _clear(w, "m7hil", "m6hil")
    assert not bad, "; ".join(bad)


@test("var.per_board", "Variables are per board: ;W2,;V and ;W2,;M! set W2 only; a ;V typed on W1 never reaches W2", needs=["wcb1"], links=[])
def per_board(bench):
    """docs/VARIABLES_DESIGN.md:129 says a broadcast ;V sets every board; a WCB console cannot originate one (only
    unprefixed text broadcasts), so that holds only for a mesh client's broadcast."""
    w = usb_wcb(bench)
    bad = []
    with Console(bench, 2) as c2:
        _clear(w, "hilw", "m7hil")
        for n in ("hilw", "m7hil"):
            _crun(c2, f"?VAR,CLEAR,{n}")
        try:
            w.send(";W2,;V,hilw,3")
            w.send(";W2,;M!m7hil=42")
            time.sleep(1.5)
            got2 = [_cget(c2, "hilw"), _cget(c2, "m7hil")]
            if got2 != ["[VAR] hilw = 3", "[VAR] m7hil = 42"]:
                bad.append(f"W2 {got2}")
            got1 = [_get(w, "hilw"), _get(w, "m7hil")]
            if got1 != [_not_set("hilw"), _not_set("m7hil")]:
                bad.append(f"W1 {got1}")
            w.run(";V,hilw,9")
            time.sleep(1.0)
            if _cget(c2, "hilw") != "[VAR] hilw = 3":
                bad.append("W1's ;V reached W2")
        finally:
            _clear(w, "hilw", "m7hil")
            for n in ("hilw", "m7hil"):
                _crun(c2, f"?VAR,CLEAR,{n}")
    assert not bad, "; ".join(bad)


@test("var.vp_mesh_persist_reboot", ";VP sent to W2 over the mesh survives a W2 reboot and rides W2's config; ;V does not (W2 reboot)", needs=["wcb1"], links=[])
def vp_mesh_persist_reboot(bench):
    w = usb_wcb(bench)
    bad = []
    with config_guard(bench, 2), Console(bench, 2) as c2:
        try:
            w.send(";W2,;VP,hilwp,6")
            w.send(";W2,;V,hilwv,2")
            time.sleep(1.5)
            tokens = snapshot(bench, 2)
            if "?VAR,SET,hilwp,6" not in tokens or _has(tokens, "hilwv"):
                bad.append(f"W2 config variables {[t for t in tokens if t.startswith('?VAR,')]}")
            m = w.send(";W2,?reboot")
            w.dev.expect(r"^\[ETM\] WCB2 came ONLINE \(boot\)", timeout=25, since=m)
            time.sleep(3)
            got = [_cget(c2, "hilwp"), _cget(c2, "hilwv")]
            if got != ["[VAR] hilwp = 6", _not_set("hilwv")]:
                bad.append(f"after the W2 reboot {got}")
        finally:
            _crun(c2, "?VAR,CLEAR,hilwp")
            _crun(c2, "?VAR,CLEAR,hilwv")
    assert not bad, "; ".join(bad)


# ============================================================ IF gating (W1 USB lines, markers on W1 S1)
FALSE = " -> false (skipping next command)"


def _gated(w, s1, cases, settle=1.0):
    """cases: [(line with {t} for a fresh marker, should the marker arrive)]. Each line goes out on its own, since
    WCB.run() would not change the chain but its sentinel adds console noise -> (sent, bytes on W1 S1, W1 lines)."""
    pm, wm = s1.mark(), w.dev.mark()
    sent = []
    for fmt, want in cases:
        t = marker()
        w.send(fmt.format(t=t))
        sent.append((fmt.format(t=t), t, want))
        time.sleep(0.15)
    time.sleep(settle)
    return sent, s1.received(pm), [x.rstrip() for x in w.dev.since(wm)]


def _arrivals(sent, got):
    return [f"{line}: marker {'missing' if want else 'arrived'}" for line, t, want in sent if (t.encode() + b"\r" in got) != want]


@test("if.operators", "IF = != < > <= >= with negative literals; an unset variable reads 0 and IF creates nothing", needs=["wcb1"])
def if_operators(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    conds = [("hila=5", True), ("hila=4", False), ("hila!=4", True), ("hila!=5", False), ("hila<6", True), ("hila<5", False),
             ("hila>4", True), ("hila>5", False), ("hila<=5", True), ("hila<=4", False), ("hila>=5", True), ("hila>=6", False),
             ("hilb<-2", True), ("hilb>-3", False), ("hilu=0", True), ("hilu=1", False), ("hilu>=0", True)]
    bad = []
    _clear(w, "hilu")
    try:
        w.run(";V,hila,5")
        w.run(";V,hilb,-3")
        sent, got, lines = _gated(w, s1, [(f"IF,{c}^;S1{{t}}", want) for c, want in conds])
        bad += _arrivals(sent, got)
        for (c, want), (_, t, _) in zip(conds, sent):
            echo = f"[IF] {c} -> true" if want else f"[IF] {c}{FALSE}"
            if echo not in lines:
                bad.append(f"no {echo!r}")
            if not want and f"[IF] skipped: ;S1{t}" not in lines:
                bad.append(f"no skipped line for {c}")
        if _get(w, "hilu") != _not_set("hilu"):
            bad.append("IF created hilu")
    finally:
        _clear(w, "hila", "hilb")
    assert not bad, "; ".join(bad)


@test("if.parse_quirks", "IF's right side is an integer literal only ('true' reads 0, '==' is '=' against 0); lowercase if and spaces work", needs=["wcb1"])
def if_parse_quirks(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    bad = []
    _clear(w, "hilu")
    try:
        w.run(";V,hilb,true")
        w.run(";V,hila,1")
        sent, got, lines = _gated(w, s1, [("IF,hilb=true^;S1{t}", False), ("IF,hilb=1^;S1{t}", True), ("IF,hilb==1^;S1{t}", False),
                                          ("IF,hilu==1^;S1{t}", True), ("if,hila=1^;S1{t}", True), ("IF, hila = 1 ^;S1{t}", True)])
        bad += _arrivals(sent, got)
        bad += [f"no {e!r}" for e in (f"[IF] hilb=true{FALSE}", f"[IF] hilb==1{FALSE}", "[IF] hilu==1 -> true", "[IF] hila=1 -> true",
                                      "[IF]  hila = 1 -> true") if e not in lines]
    finally:
        _clear(w, "hilb", "hila")
    assert not bad, "; ".join(bad)


@test("if.compound", "AND/OR in any case, evaluated strictly left to right with no AND precedence", needs=["wcb1"])
def if_compound(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    bad = []
    try:
        for cmd in (";V,hila,1", ";V,hilb,0", ";V,hilc,0"):
            w.run(cmd)
        sent, got, lines = _gated(w, s1, [("IF,hila=1,AND,hilb=0^;S1{t}", True), ("IF,hila=1,AND,hilb=1^;S1{t}", False),
                                          ("IF,hila=0,OR,hilb=0^;S1{t}", True), ("IF,hila=0,OR,hilb=1^;S1{t}", False),
                                          ("IF,hila=1,and,hilb=0^;S1{t}", True), ("IF,hila=1,OR,hilb=1,AND,hilc=1^;S1{t}", False)])
        bad += _arrivals(sent, got)
        # (T or F) and F: with AND precedence it would be T or (F and F) = true.
        bad += [f"no {e!r}" for e in ("[IF] hila=1,AND,hilb=0 -> true", f"[IF] hila=1,OR,hilb=1,AND,hilc=1{FALSE}") if e not in lines]
    finally:
        _clear(w, "hila", "hilb", "hilc")
    assert not bad, "; ".join(bad)


@test("if.malformed_fails_closed", "Malformed IF expressions print a [VAR] IF: reason and skip the next command", needs=["wcb1"])
def if_malformed_fails_closed(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    cases = [("hila", "[VAR] IF: no operator in 'hila'"), ("=5", "[VAR] IF: missing variable in '=5'"),
             ("AND,hila=1", "[VAR] IF: misplaced AND"), ("hila=1,AND", "[VAR] IF: trailing AND"),
             ("hila=1,hilb=0", "[VAR] IF: two conditions without AND/OR between them"), ("", "[VAR] IF: empty condition"),
             (",", "[VAR] IF: no condition"), ("hila=1,AND,OR,hilb=0", "[VAR] IF: misplaced OR")]
    bad = []
    try:
        w.run(";V,hila,1")
        w.run(";V,hilb,0")
        sent, got, lines = _gated(w, s1, [(f"IF,{c}^;S1{{t}}", False) for c, _ in cases], settle=1.5)
        bad += _arrivals(sent, got)
        reasons = [x for x in lines if x.startswith("[VAR] IF:")]
        if reasons != [r for _, r in cases]:
            bad.append(f"reasons {reasons}")
        bad += [f"no false echo for {c!r}" for c, _ in cases if f"[IF] {c}{FALSE}" not in lines]
    finally:
        _clear(w, "hila", "hilb")
    assert not bad, "; ".join(bad)


@test("if.malformed_or_passes", "(should) A malformed condition OR'd with a true one still fails closed and skips the command", needs=["wcb1"])
def if_malformed_or_passes(bench):
    """WCB_Variables.h:54 promises a malformed expression returns false (fail-safe: skip). evalOneCondition only fails
    that one term, and evaluateIfCondition ORs it with the rest (WCB_Variables.cpp:456-459), so the command runs."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    try:
        w.run(";V,hila,1")
        sent, got, lines = _gated(w, s1, [("IF,hila=1,OR,junk^;S1{t}", False)])
    finally:
        _clear(w, "hila")
    bench.note(f"IF,hila=1,OR,junk printed {[x for x in lines if x.startswith(('[IF]', '[VAR]'))]}")
    assert not _arrivals(sent, got), "the command gated by a malformed OR expression ran"


@test("if.gate_scope", "A false IF skips exactly the next token; comments do not use up the gate; consecutive IFs AND together", needs=["wcb1"])
def if_gate_scope(bench):
    """Code over doc: docs/VARIABLES_DESIGN.md:165-166 says consecutive IFs do not mean AND; the code
    (WCB_Variables.cpp:391-409) and the same doc's section 3 (:110-112) say they do."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    m = {k: marker(k) for k in ("a", "b", "c", "f", "d", "e", "x", "y", "g")}
    bad = []
    _clear(w, "hilu")
    try:
        w.run(";V,hila,1")
        w.run(";V,hilb,0")
        pm, wm = s1.mark(), w.dev.mark()
        for line in (f";S1{m['a']}^IF,hilu=1^;S1{m['b']}^;S1{m['c']}", f"IF,hilu=1^***note^;S1{m['f']}^;S1{m['d']}",
                     f"IF,hilu=0^***note^;S1{m['e']}", f"IF,hila=1^IF,hilb=1^;S1{m['x']}", f"IF,hila=0^IF,hilb=0^;S1{m['y']}",
                     f"IF,hila=1^IF,hilb=0^;S1{m['g']}"):
            w.send(line)
            time.sleep(0.2)
        time.sleep(1.0)
        got, lines = s1.received(pm), [x.rstrip() for x in w.dev.since(wm)]
        want = b"".join(m[k].encode() + b"\r" for k in ("a", "c", "d", "e", "g"))
        if got != want:
            bad.append(f"W1 S1 got {got!r}, expected {want!r}")
        bad += [f"no {e!r}" for e in (f"[IF] skipped: ;S1{m['b']}", "Ignored chain command: ***note", f"[IF] skipped: ;S1{m['f']}",
                                      "[IF] hila=1 -> true", f"[IF] hilb=1{FALSE}", f"[IF] skipped: ;S1{m['x']}", f"[IF] hila=0{FALSE}",
                                      "[IF] skipped (earlier condition false): IF,hilb=0", f"[IF] skipped: ;S1{m['y']}") if e not in lines]
    finally:
        _clear(w, "hila", "hilb")
    assert not bad, "; ".join(bad)


@test("if.gate_timers", "A false IF drops a bare ;T and keeps gating; ';T800,cmd' is dropped and uses up the gate; a true IF keeps delays", needs=["wcb1"])
def if_gate_timers(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    bad = []

    def run(line_fmt, keys, wait):
        mk = {k: marker(k) for k in keys}
        pm, wm = s1.mark(), w.dev.mark()
        w.send(line_fmt.format(**mk))
        time.sleep(wait)
        times = {k: s1.time_of(v.encode(), pm) for k, v in mk.items()}
        return mk, times, [x.rstrip() for x in w.dev.since(wm)]

    _clear(w, "hilu")
    try:
        mk, tm, lines = run(";S1{a}^IF,hilu=1^;T800^;S1{b}^;T300^;S1{c}", "abc", 1.5)
        bad += [f"run 1: no {e!r}" for e in (f"[IF] hilu=1{FALSE}", "[IF] skipped delay: ;T800", f"[IF] skipped: ;S1{mk['b']}") if e not in lines]
        if tm["b"] is not None or tm["a"] is None or tm["c"] is None or not 250 <= tm["c"] - tm["a"] <= 600:
            bad.append(f"run 1 probe times {tm}")
        mk, tm, lines = run(";S1{a}^IF,hilu=1^;T800,;S1{b}^;S1{c}", "abc", 1.5)
        if f"[IF] skipped: ;T800,;S1{mk['b']}" not in lines:
            bad.append("run 3: the ;T800,cmd token was not the one skipped")
        if tm["b"] is not None or tm["a"] is None or tm["c"] is None or tm["c"] - tm["a"] >= 250:
            bad.append(f"run 3 probe times {tm}")
        w.run(";V,hilu,1")
        mk, tm, lines = run(";S1{a}^IF,hilu=1^;T800^;S1{b}^;T300^;S1{c}", "abc", 2.0)
        if None in tm.values() or not 700 <= tm["b"] - tm["a"] <= 1100 or not 250 <= tm["c"] - tm["b"] <= 600:
            bad.append(f"run 2 probe times {tm}")
    finally:
        _clear(w, "hilu")
    assert not bad, "; ".join(bad)


@test("if.invoke_time_timer", "An IF in a timer chain is decided at parse time; changing the variable during the delay does not cancel", needs=["wcb1"])
def if_invoke_time_timer(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    a, t = marker("a"), marker("t")
    try:
        w.run(";V,hila,1")
        pm, wm = s1.mark(), w.dev.mark()
        w.send(f";S1{a}^IF,hila=1^;T1000^;S1{t}")
        time.sleep(0.2)
        w.send(";V,hila,0")
        s1.expect(t.encode() + b"\r", timeout=3, since=pm)
        gap = s1.time_of(t.encode(), pm) - s1.time_of(a.encode(), pm)
        lines = [x.rstrip() for x in w.dev.since(wm)]
    finally:
        _clear(w, "hila")
    assert "[IF] hila=1 -> true" in lines, "no true echo"
    assert 900 <= gap <= 1300, f"delay {gap} ms"


@test("if.same_chain_stale", "Characterization: a set-and-test in one chain normally reads the old value", needs=["wcb1"])
def if_same_chain_stale(bench):
    """docs/VARIABLES_DESIGN.md:116-118 says IF is evaluated before any command of its chain runs. That is only typical:
    serialCommandTask and loop() share core 1 at priority 1 (WCB.ino:8174), so loop() can apply the ;V first."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    stale = False
    try:
        for attempt in range(2):
            _clear(w, "hilu")
            sent, got, lines = _gated(w, s1, [(";V,hilu,1^IF,hilu=1^;S1{t}", False)])
            stale = not _arrivals(sent, got) and f"[IF] hilu=1{FALSE}" in lines
            if stale:
                break
            bench.note(f"same-chain attempt {attempt + 1}: loop() applied ;V before IF was evaluated")
        value = _get(w, "hilu")
        sent, got, _ = _gated(w, s1, [("IF,hilu=1^;S1{t}", True)])
    finally:
        _clear(w, "hilu")
    bench.note(f"same-chain IF read the old value: {stale}")
    assert value == "[VAR] hilu = 1", f"hilu after the chain: {value!r}"
    assert not _arrivals(sent, got), "a later IF did not see the new value"


@test("if.payload_rejections", "IF as a ;T payload or inside a ;W payload is rejected with its error; the next command runs ungated", needs=["wcb1"])
def if_payload_rejections(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    route = "[IF] ERROR: IF cannot be routed inside ;w — put it before the route instead: IF,cond^;w{n},command. Command ignored."
    _clear(w, "hila")
    a, b, c = marker("a"), marker("b"), marker("c")
    pm, wm = s1.mark(), w.dev.mark()
    w.send(f";S1{a}^;T200,IF,hila=1^;S1{b}")
    time.sleep(1.0)
    lines = [x.rstrip() for x in w.dev.since(wm)]
    bad = []
    if "[IF] ERROR: IF cannot be a timer payload (';t200,IF,hila=1') — write it as IF,cond^;t200^command instead. Token ignored." not in lines:
        bad.append("no timer-payload error")
    ta, tb = s1.time_of(a.encode(), pm), s1.time_of(b.encode(), pm)
    if ta is None or tb is None or not 150 <= tb - ta <= 450:
        bad.append(f"<b> timing {ta}, {tb}")
    wm = w.dev.mark()
    w.send(f";W2,IF,hila=1^;S1{c}")
    time.sleep(1.0)
    if route.format(n=2) not in [x.rstrip() for x in w.dev.since(wm)]:
        bad.append(";W2,IF not rejected")
    if c.encode() + b"\r" not in s1.received(pm):
        bad.append("the command after the rejected route did not run")
    out = [x.rstrip() for x in w.run(";W9,IF,hila=1")]
    if route.format(n=9) not in out or _has(out, "is not a reachable target"):
        bad.append(f";W9,IF printed {out}")
    if route.format(n=2) not in [x.rstrip() for x in w.run(";W2IF,hila=1")]:
        bad.append(";W2IF not rejected")
    assert not bad, "; ".join(bad)


@test("if.gates_route", "An IF placed before a ;W route gates the whole route", needs=["wcb1"])
def if_gates_route(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    t, f = marker("t"), marker("f")
    try:
        w.run(";V,hila,1")
        m = s2.mark()
        w.send(f"IF,hila=1^;W2,;S2{t}")
        s2.expect(t.encode() + b"\r", timeout=3, since=m)
        m, wm = s2.mark(), w.dev.mark()
        w.send(f"IF,hila=2^;W2,;S2{f}")
        time.sleep(3)
        got, lines = s2.received(m), [x.rstrip() for x in w.dev.since(wm)]
    finally:
        _clear(w, "hila")
    assert f"[IF] skipped: ;W2,;S2{f}" in lines, "no skipped line for the route"
    assert f.encode() not in got, "the gated route still reached W2"


@test("if.gates_seq_save", "IF gates a whole ?SEQ,SAVE token including its ^-chained value", needs=["wcb1"])
def if_gates_seq_save(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    x, y = marker("x"), marker("y")
    value = f";S1{x}^;S1{y}"
    bad = []
    with config_guard(bench, 1):
        _clear(w, "hilu")
        w.run("?SEQ,CLEAR,HILG")
        try:
            pm, wm = s1.mark(), w.dev.mark()
            w.send(f"IF,hilu=1^?SEQ,SAVE,HILG,{value}")
            time.sleep(0.8)
            if f"[IF] skipped: ?SEQ,SAVE,HILG,{value}" not in [z.rstrip() for z in w.dev.since(wm)]:
                bad.append("the whole ?SEQ,SAVE token was not skipped")
            if not _has(w.run("?SEQ,GET,HILG"), "[MGMT:SEQVAL,1]HILG,NOTFOUND,"):
                bad.append("the skipped save stored something")
            w.run(";V,hilu,1")
            wm = w.dev.mark()
            w.send(f"IF,hilu=1^?SEQ,SAVE,HILG,{value}")
            time.sleep(0.8)
            if f"Stored: Key='HILG', Value='{value}'" not in [z.rstrip() for z in w.dev.since(wm)]:
                bad.append("the gated save did not store the full value")
            if not _has(w.run("?SEQ,GET,HILG"), f"[MGMT:SEQVAL,1]HILG,OK,{value}"):
                bad.append("GET after the save")
            if s1.received(pm):
                bad.append(f"the saved value ran: {s1.received(pm)!r}")
        finally:
            w.run("?SEQ,CLEAR,HILG")
            _clear(w, "hilu")
    assert not bad, "; ".join(bad)


@test("if.timer_split_trap", "(should) An IF-gated ?SEQ,SAVE whose value holds ;T stores the whole value and runs none of it", needs=["wcb1"])
def if_timer_split_trap(bench):
    """Probable bug: the exemption that keeps ?SEQ,SAVE values away from the naive timer splitter (WCB.ino:7155-7163)
    applies only when the LINE starts with '?'. A chain putting ?SEQ,SAVE after a non-'?' token, with ;T in the value,
    stores a truncated value and runs the tail — as do ETM-received and recalled chains (WCB.ino:4454, WCB_Storage.cpp:625)."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    a, b = marker("a"), marker("b")
    value = f";S1{a}^;T400^;S1{b}"
    with config_guard(bench, 1):
        _clear(w, "hilu")
        w.run("?SEQ,CLEAR,HILTS")
        try:
            pm = s1.mark()
            w.send(f"IF,hilu=0^?SEQ,SAVE,HILTS,{value}")
            time.sleep(1.5)
            stored = [x.rstrip() for x in w.run("?SEQ,GET,HILTS") if x.startswith("[MGMT:SEQVAL")]
            ran = s1.received(pm)
            w.run(f"?SEQ,SAVE,HILTS,{value}")
            control = [x.rstrip() for x in w.run("?SEQ,GET,HILTS") if x.startswith("[MGMT:SEQVAL")]
        finally:
            w.run("?SEQ,CLEAR,HILTS")
    assert control == [f"[MGMT:SEQVAL,1]HILTS,OK,{value}"], f"control save (line starting '?') {control}"
    assert stored == [f"[MGMT:SEQVAL,1]HILTS,OK,{value}"] and not ran, f"IF-gated save stored {stored}, ran {ran!r}"


@test("if.mesh_frag_target_vars", "An IF chain run on W2 through a one-chunk ?MGMT,FRAG tests W2's variables, not W1's", needs=["wcb1"])
def if_mesh_frag_target_vars(bench):
    s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    t, f = marker("t"), marker("f")
    try:
        w.send(";W2,;V,hilw,3")
        w.run(";V,hilw,0")
        time.sleep(1.0)
        m, wm = s2.mark(), w.dev.mark()
        w.send(f"?MGMT,FRAG,2,{nonce()[:4]},0,1,IF,hilw=3^;S2{t}")
        s2.expect(t.encode() + b"\r", timeout=3, since=m)
        w.send(f"?MGMT,FRAG,2,{nonce()[:4]},0,1,IF,hilw=0^;S2{f}")
        time.sleep(3)
        got, lines = s2.received(m), w.dev.since(wm)
    finally:
        w.send(";W2,?VAR,CLEAR,hilw")
        _clear(w, "hilw")
    assert f.encode() not in got, "W2 evaluated the IF against W1's value"
    assert not [x for x in lines if x.startswith("[IF]")], "the IF was evaluated on W1"


@test("if.stored_seq_recall_time", "An IF inside a stored sequence is saved verbatim and evaluated at each recall", needs=["wcb1"])
def if_stored_seq_recall_time(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    t = marker("t")
    bad = []
    with config_guard(bench, 1):
        _clear(w, "hilu")
        w.run("?SEQ,CLEAR,HILIF")
        try:
            out = [x.rstrip() for x in w.run(f"?SEQ,SAVE,HILIF,IF,hilu=1^;S1{t}")]
            if f"Stored: Key='HILIF', Value='IF,hilu=1^;S1{t}'" not in out or [x for x in out if x.startswith("[IF]")]:
                bad.append(f"save printed {out}")
            sent, got, lines = _gated(w, s1, [(";CHILIF,L", False)])
            if t.encode() in got or f"[IF] hilu=1{FALSE}" not in lines:
                bad.append("recall 1 did not skip")
            w.run(";V,hilu,1")
            pm, wm = s1.mark(), w.dev.mark()
            w.send(";CHILIF,L")
            try:
                s1.expect(t.encode() + b"\r", timeout=2, since=pm)
            except AssertionError:
                bad.append("recall 2 did not run the command")
            # The echo can print AFTER the command's bytes reach S1 (93 ms later on 2026-09-21), so wait for it.
            try:
                w.dev.expect(r"^\[IF\] hilu=1 -> true", timeout=2, since=wm)
            except AssertionError:
                bad.append("recall 2 printed no true echo")
        finally:
            w.run("?SEQ,CLEAR,HILIF")
            _clear(w, "hilu")
    assert not bad, "; ".join(bad)
