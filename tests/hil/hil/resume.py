"""Resume checks: re-establish the bench before a paused or interrupted run continues (docs/HIL_TESTING.md §9).

check_bench() runs, in order:
  1. offline differences (no ports): checkpoint format, host, bench.json, wiring, test code, dropped test ids
  2. reload bench.json and links.json (Find devices or Auto-detect may have run while paused)
  3. re-identify every USB device - board number / MAC / kind plus the USB serial recorded at the start; never a guess
  4. after a cut-off test: reboot the WCBs and zero NaviCore's debug flags (that test's cleanup never ran)
  5. wait for the mesh: every bench WCB online from WCB1 (and from NaviCore), then a short settle for WDP adverts
  6. firmware versions against the last segment's
  7. saved config against the run's reference (config_ref), in redacted form
  8. probes: RESET, and out of the mesh if a cut-off test left one joined
  9. every wire that was verified: re-checked at its port's configured baud, in its recorded orientation, never
     rediscovered
 10. NaviCore PING and the SBUS controller's JSON ping; after a cut-off sbus.* test, the controller's held sticks,
     buttons and button-mode trims are released

A hard block raises ResumeBlocked (the message is what to fix). A soft difference calls ask(title, text, default) and
continues only on True; ask=None - the automatic resume after a host outage - turns every soft difference into a
block. should_abort() (Stop, Pause or Ctrl+C) is checked between steps and raises ResumeAborted: the run stays paused.
"""
import json
import os
import socket
import time
from types import SimpleNamespace

from serial.tools import list_ports

from . import checkpoint as ck
from . import identify
from .config import read_config
from .links import LinkManager
from .navicore import NaviCore
from .probe import Probe
from .serialdev import ExpectTimeout
from .wcb import WCB

HIL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MESH_WAIT_S = 45
MESH_SETTLE_S = 3            # boot-time WDP adverts land in the first seconds after ETM sees a board
IDENTIFY_WAIT_S = 20         # boards boot and USB re-enumerates after a power cycle
PROBE_WAIT_S = 10
NAVICORE_WAIT_S = 10
SBUS_WAIT_S = 60             # the SBUS controller may reset when its port opens (HIL_TESTING §2)


class ResumeBlocked(Exception):
    """The bench is not fit to continue. The message says what to fix."""


class ResumeAborted(Exception):
    """Stop, Pause or Ctrl+C during the checks. The run stays paused; not an error."""


class ResumeDeclined(ResumeAborted):
    """A difference was shown and the answer was No."""


def _first(e):
    """The first line of an error, with any mesh password or WiFi credential it quotes redacted: a split_checked_chain
    or ExpectTimeout message can carry a backup chain, and these lines reach session.log, the checkpoint and the
    report."""
    return ck.redact_text((str(e).splitlines() or [type(e).__name__])[0])


def _abort(should_abort):
    if should_abort and should_abort():
        raise ResumeAborted("resume cancelled")


def _confirm(ask, title, lines, default=False):
    text = "\n".join(lines)
    if ask is None:
        raise ResumeBlocked(f"{title}:\n{text}\n(An automatic resume after a host outage never accepts a difference - "
                            f"resume by hand.)")
    if not ask(title, text, default):
        raise ResumeDeclined(f"{title} - declined")


def cutoff_hint(ckpt):
    """What a test cut off mid-way may have left behind that stops a board answering, from its area."""
    tid = ckpt.in_flight
    if not tid:
        return ""
    area = tid.split(".")[0]
    what = {"chars": "its command character, LFI or delimiter", "ota": "its USB baud",
            "etm": "ETM, the mesh channel or the password", "wdp": "ETM, the mesh channel or the password"}.get(
        area, "its state")
    if area == "chars":   # both are saved in NVS (WCB_Storage.cpp), so a power cycle keeps them
        return f" The cut-off test {tid} may have changed {what}, which a power cycle keeps - restore it by hand."
    return (f" The cut-off test {tid} may have changed {what}. Power-cycle it, and if that does not help, restore it "
            f"by hand.")


def _chars_block(name, port, info):
    """A ResumeBlocked naming the exact restore command when a WCB's LFI, command character or delimiter is not the
    default. Raised at once, with no boot-wait retry: every later step would send '?'/';' lines that such a board
    broadcasts, and run()'s ';S0,HILEND' sentinel splits at a ',' delimiter."""
    lfi, cc, dl = info.get("lfi") or "?", info.get("cmdchar") or ";", info.get("delim") or "^"
    fixes = []
    if dl != "^":
        # First, and in the legacy spelling (WCB.ino, ?D<x>): '?DELIM,^' and the other restores contain a ',', which
        # a ',' delimiter splits.
        fixes.append(f"its delimiter is '{dl}' - restore it first with {lfi}D^")
    if cc != ";":
        fixes.append(f"its command character is '{cc}' - restore it with {lfi}CMDCHAR,;")
    if lfi != "?":
        fixes.append(f"its LFI is '{lfi}' - restore it with {lfi}FUNCCHAR,?")
    return ResumeBlocked(f"{name} on {port}: " + "; ".join(fixes) + " on its USB (all three are saved in NVS, so a "
                         f"power cycle keeps them). Then resume again.")


def _chars_changed(info):
    return bool(info) and info.get("kind") == "wcb" and (
        (info.get("lfi") or "?") != "?" or (info.get("cmdchar") or ";") != ";" or (info.get("delim") or "^") != "^")


def _mesh_hint(ckpt):
    tid = ckpt.in_flight or ""
    if tid.split(".")[0] in ("etm", "wdp"):
        return f" The cut-off test {tid} may have changed ETM, the channel or the password."
    return ""


# ---------------------------------------------------------------------------- snapshots (also used at a run's start)
def usb_map(bench):
    """{device: {port, serial, vid, pid, location}} from the USB descriptors. Enumerates only - no traffic."""
    ports = {p.device: p for p in list_ports.comports()}
    return {name: dict(port=d.get("port"), **identify.usb_fingerprint(d.get("port"), ports))
            for name, d in bench.cfg["devices"].items()}


def snapshot_configs(bench):
    """{wcb: redacted comparable tokens, or None where the board could not be read}. Best effort per board."""
    out = {}
    if not bench.has("wcb1"):
        return out
    for w in bench.wcb_numbers():
        try:
            toks = read_config(bench, w)
            bench.cache[w] = toks
            out[str(w)] = ck.redact_tokens(toks)
        except Exception as e:  # noqa: BLE001 - a snapshot never fails the run
            bench.note(f"config snapshot of W{w} failed ({_first(e)}) - its previous reference is kept")
            out[str(w)] = None
    return out


def record_firmware(bench):
    """{device: version} at a run's start: ?VERSION on each USB WCB, PING on NaviCore, HELLO on each probe. The SBUS
    controller is not touched (it may reset on open); errors record None."""
    fw = {}
    for name, d in bench.cfg["devices"].items():
        try:
            if d["kind"] == "wcb":
                fw[name] = WCB(bench.dev(name)).version()
            elif d["kind"] == "navicore":
                fw[name] = NaviCore(bench.dev(name)).ping()
            elif d["kind"] == "probe":
                fw[name] = Probe(bench.dev(name)).hello().group(1)
            else:
                fw[name] = None
        except Exception as e:  # noqa: BLE001
            fw[name] = None
            bench.note(f"firmware of {name} not recorded ({_first(e)})")
    return fw


# ---------------------------------------------------------------------------- step 1
def offline_differences(bench, ckpt):
    """(soft, hard, notes, restore_links) without opening a port."""
    from .runner import REGISTRY
    d = ckpt.data
    soft, hard, notes, restore = [], [], [], False
    if d.get("format") != ck.FORMAT:
        hard.append(f"This checkpoint is format {d.get('format')}; this harness reads format {ck.FORMAT} - update the "
                    f"harness.")
        return soft, hard, notes, restore
    last_host = next((s.get("host") for s in reversed(d["segments"]) if s.get("host")), None)
    host = socket.gethostname()
    if last_host and last_host != host:
        soft.append(f"host: {last_host} -> {host}")
    try:
        with open(bench.bench_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        hard.append(f"bench.json could not be read ({e})")
        return soft, hard, notes, restore
    diffs = ck.diff_canon((d.get("bench") or {}).get("canon"), ck.bench_canon(cfg))
    if diffs:
        soft.append("bench.json: " + "; ".join(diffs))
    rec_links = (d.get("links") or {}).get("canon") or []
    if not os.path.exists(bench.links.path):
        if rec_links:
            soft.append(f"results/links.json is missing - Yes restores this run's {len(rec_links)} wire(s) from the "
                        f"checkpoint, and each one must then verify")
            restore = True
    else:
        mgr = LinkManager(SimpleNamespace(cfg=cfg, note=lambda s: None), bench.links.path)
        mgr.load()
        cur, _ = ck.links_canon(mgr)
        ld = ck.diff_links(rec_links, cur)
        if ld:
            soft.append("wiring: " + "; ".join(ld))
    rec_h = d.get("harness") or {}
    rec_files = rec_h.get("files") or {}
    cur_files = ck.harness_files(HIL_ROOT)
    changed = sorted(k for k in set(rec_files) | set(cur_files) if rec_files.get(k) != cur_files.get(k))
    if rec_files and changed:
        now = ck.harness_info(HIL_ROOT)
        soft.append(f"test code changed in {len(changed)} file(s): {', '.join(changed[:12])}"
                    f"{' ...' if len(changed) > 12 else ''} (git {rec_h.get('git')} -> {now.get('git')})")
    if rec_h.get("python") or rec_h.get("pyserial"):
        now = ck.harness_info(HIL_ROOT)
        for key in ("python", "pyserial"):
            if rec_h.get(key) and now.get(key) and rec_h[key] != now[key]:
                soft.append(f"{key}: {rec_h[key]} -> {now[key]}")
    before = set(d.get("dropped") or [])
    _, dropped = ckpt.remaining(REGISTRY)
    new = [t for t in dropped if t not in before]
    if new:
        soft.append(f"no longer in the suite (renamed or removed), so not run: {', '.join(new)}")
    if ckpt.in_flight:
        notes.append(f"the run was cut off during {ckpt.in_flight}: it runs again first")
    return soft, hard, notes, restore


# ---------------------------------------------------------------------------- step 3
def _diagnose(name, want, port, info, hint):
    got = identify.identity_from(info)
    if info is None:
        return (f"{name} is on {port} (same USB board) but does not answer at 115200 (WCB_WEBTOOL_CONFIG_PULL)."
                + (hint or " Power-cycle it."))
    if info.get("kind") == "error":
        return (f"{port} could not be opened: {info.get('error')} - close the Arduino IDE monitor / Wizard tab / "
                f"another WCB Bench holding it")
    if info.get("kind") == "wcb" and info.get("wcb") is None:
        return (f"{name} is on {port} (same USB board) and answers, but its board number could not be read."
                + (hint or " Power-cycle it."))
    return f"{port} carries {name}'s USB serial but answers as {identify.describe(got)}, not {identify.describe(want)}"


def reidentify(bench, ckpt, log, should_abort=None, ask=None, wait_s=None):
    """Find every bench USB device again (design §6, corrected: identity is pinned by the recorded USB serial).

    A WCB, NaviCore or SBUS with a recorded, unique USB serial is looked up by that serial first (no traffic), then its
    board number is confirmed with identify_port(wcb_home=True) on that port only - WCB_WEBTOOL_CONFIG_PULL first, so
    nothing is broadcast whatever its LFI; NaviCore and SBUS are confirmed by the serial and proven by the step-10 ping.
    A WCB whose LFI or command character is not the default blocks at once, naming the restore command: every later
    step sends '?'/';' lines such a board would broadcast. A port that answers with the right identity but a different
    serial is never adopted automatically - it is asked about (blocked when ask is None). A probe is identified by its
    MAC (unique), the serial only says which port to try first. A device with no usable serial falls back to Find
    devices' rule: the one port that answers with its identity. Returns (moves, versions)."""
    wait_s = IDENTIFY_WAIT_S if wait_s is None else wait_s
    devices = bench.cfg["devices"]
    recorded = ckpt.data.get("devices") or {}
    hint = cutoff_hint(ckpt)
    for name, d in devices.items():
        if d.get("kind") == "probe" and not d.get("mac"):
            raise ResumeBlocked(f"{name} has no mac in bench.json, so it cannot be told apart from another probe - run "
                                f"Find devices once (Devices tab), then resume")
    counts = {}
    for r in recorded.values():
        if r.get("serial"):
            counts[r["serial"]] = counts.get(r["serial"], 0) + 1

    def serial_of(name):
        s = (recorded.get(name) or {}).get("serial")
        return s if s and counts.get(s) == 1 else None

    confirmed, versions, noted = {}, {}, set()
    deadline = time.monotonic() + wait_s
    while True:
        _abort(should_abort)
        ports = identify.usb_ports()
        cache = {}

        def ident(p, wcb_home=False):
            if (p, wcb_home) not in cache:
                cache[(p, wcb_home)] = (identify.identify_port(p, wcb_home=True) if wcb_home
                                        else identify.identify_port(p))
            return cache[(p, wcb_home)]

        problems, offers, errors, scan = {}, {}, {}, []
        claimed = set(confirmed.values())
        serial_homes = {}
        for name in devices:
            s = serial_of(name)
            if s:
                serial_homes[name] = [p for p, i in ports.items() if (i.serial_number or None) == s]
        reserved = {p for n, hs in serial_homes.items() if n not in confirmed for p in hs}
        for name, d in devices.items():
            if name in confirmed:
                continue
            want = identify.identity_of(d)
            strict = d["kind"] in ("wcb", "navicore", "sbus") and serial_of(name) is not None
            homes = [p for p in serial_homes.get(name, []) if p not in claimed]
            if strict:
                if len(homes) > 1:
                    problems[name] = (f"{', '.join(homes)} all carry {name}'s USB serial {serial_of(name)} - unplug the "
                                      f"one that is not the bench's")
                    continue
                if not homes:
                    scan.append(name)
                    continue
                home = homes[0]
                if d["kind"] in ("navicore", "sbus"):
                    confirmed[name] = home
                    claimed.add(home)
                    continue
                # The recorded USB serial already proves this is the bench WCB: identify it with the fixed
                # WCB_WEBTOOL_CONFIG_PULL first, which answers whatever the LFI and command character are.
                info = ident(home, wcb_home=True)
                if _chars_changed(info):
                    raise _chars_block(name, home, info)
                if identify.identity_from(info) == want and want[1] is not None:
                    confirmed[name] = home
                    claimed.add(home)
                    versions[name] = info.get("version")
                    continue
                problems[name] = _diagnose(name, want, home, info, hint)
                continue
            if name not in noted and d["kind"] != "probe":
                noted.add(name)
                log(f"{name}: no unique USB serial recorded for it - identified by what answers, as Find devices does")
            first = homes[0] if homes else d.get("port")
            if first in ports and first not in claimed:
                info = ident(first)
                if identify.identity_from(info) == want and _chars_changed(info):
                    raise _chars_block(name, first, info)
                if identify.identity_from(info) == want:
                    confirmed[name] = first
                    claimed.add(first)
                    versions[name] = info.get("version")
                    continue
                if info and info.get("kind") == "error":
                    errors[first] = info.get("error")
            scan.append(name)
        if scan:
            answers = {}
            for p in sorted(ports):
                if p in claimed or p in reserved:
                    continue
                _abort(should_abort)
                info = ident(p)
                if info and info.get("kind") == "error":
                    errors[p] = info.get("error")
                got = identify.identity_from(info)
                if got:
                    answers.setdefault(got, []).append(p)
            for name in scan:
                d = devices[name]
                want = identify.identity_of(d)
                matches = answers.get(want, [])
                if serial_of(name) and d["kind"] != "probe":
                    if matches:
                        was = serial_of(name)
                        now = ports[matches[0]].serial_number
                        offers[name] = (matches[0], f"{matches[0]} answers as {identify.describe(want)} but is not the "
                                                    f"board recorded at the start (USB serial {now}, was {was}) - use "
                                                    f"it anyway?")
                    else:
                        problems[name] = (f"{name} ({identify.describe(want)}, USB serial {serial_of(name)}) is not on "
                                          f"any USB port - plug it in")
                elif len(matches) == 1:
                    if _chars_changed(cache[(matches[0], False)]):
                        raise _chars_block(name, matches[0], cache[(matches[0], False)])
                    confirmed[name] = matches[0]
                    claimed.add(matches[0])
                    versions[name] = cache[(matches[0], False)].get("version")
                    log(f"{name}: found on {matches[0]} ({identify.describe(want)})")
                elif len(matches) > 1:
                    problems[name] = (f"two ports answer as {identify.describe(want)}: {', '.join(matches)} - unplug "
                                      f"the one that is not the bench's")
                else:
                    busy = "".join(f"; {p} could not be opened: {e} - close the Arduino IDE monitor / Wizard tab / "
                                   f"another WCB Bench" for p, e in sorted(errors.items()))
                    problems[name] = f"{name} ({identify.describe(want)}) is not on any USB port - plug it in{busy}"
        pending = [n for n in devices if n not in confirmed]
        if not pending:
            break
        if time.monotonic() < deadline:
            time.sleep(2.0)
            continue
        if problems:
            lines = [problems[n] for n in pending if n in problems]
            lines += [offers[n][1] for n in pending if n in offers]
            raise ResumeBlocked("\n".join(lines))
        for name in pending:
            if name not in offers:
                raise ResumeBlocked(f"{name} could not be identified")
            port, text = offers[name]
            _confirm(ask, f"A different {identify.describe(identify.identity_of(devices[name]))}", [text])
            confirmed[name] = port
            versions[name] = (cache.get((port, False)) or {}).get("version")
            log(f"{name}: {port} accepted although its USB serial differs")
        break
    moves = []
    for name, port in confirmed.items():
        old = devices[name].get("port")
        if (old or "").upper() != port.upper():
            devices[name]["port"] = port
            moves.append(f"{name} {old} -> {port}")
    if moves:
        bench.save_config()
        log("moved: " + "; ".join(moves) + " (saved to bench.json)")
    return moves, versions


# ---------------------------------------------------------------------------- steps 4, 5, 8, 9, 10
def reboot_wcbs(bench, log, ckpt=None):
    """Reboot every WCB (mesh-only ones through WCB1 first) and zero NaviCore's debug flags: a test killed mid-way
    skipped its finally, so RAM-only toggles may still be on if the boards kept power."""
    hint = cutoff_hint(ckpt) if ckpt else ""
    usb = bench.usb_wcbs()
    mesh = [w for w in bench.wcb_numbers() if w not in usb]
    if mesh and bench.has("wcb1"):
        dev = bench.dev("wcb1")
        for n in mesh:
            m = WCB(dev).send(f";W{n},?reboot")
            try:
                dev.expect(rf"\[ETM\] WCB{n} came ONLINE", timeout=40, since=m)
                log(f"WCB{n} rebooted (over the mesh)")
            except ExpectTimeout:
                log(f"WCB{n}: no 'came ONLINE' within 40 s after ;W{n},?reboot - the mesh wait decides")
    for n, name in sorted(usb.items()):
        try:
            WCB(bench.dev(name)).reboot()
            log(f"{name} rebooted")
        except Exception as e:  # noqa: BLE001
            raise ResumeBlocked(f"{name} did not reboot cleanly ({_first(e)}).{hint}")
    if bench.has("navicore"):
        try:
            NaviCore(bench.dev("navicore")).set_debug_flags(0)
            log("NaviCore debug flags set to 0")
        except Exception as e:  # noqa: BLE001
            log(f"NaviCore debug flags not reset ({_first(e)}) - the controller check decides")


def wait_mesh(bench, ckpt, log, should_abort=None, timeout=None):
    """Every bench WCB online from WCB1 (ETM ?STATS) and from NaviCore, within `timeout`. This is also the only
    presence check a mesh-only WCB gets."""
    timeout = MESH_WAIT_S if timeout is None else timeout
    if not bench.has("wcb1"):
        return
    hint = _mesh_hint(ckpt)
    primary = bench.usb_wcb_number()
    others = [w for w in bench.wcb_numbers() if w != primary]
    deadline = time.monotonic() + timeout
    off, last = list(others), None
    while others:
        _abort(should_abort)
        try:
            stats = WCB(bench.dev("wcb1")).etm_board_stats()
            off = [w for w in others if not stats.get(w, {}).get("online")]
        except AssertionError as e:
            last = e
        if not off:
            log(f"mesh: every bench WCB is online from WCB{primary}")
            break
        if time.monotonic() >= deadline:
            raise ResumeBlocked(f"{', '.join(f'WCB{w}' for w in off)} is not online from WCB{primary} after {timeout} s "
                                f"- powered? same channel?{hint}" + (f" ({_first(last)})" if last else ""))
        time.sleep(3)
    if bench.has("navicore"):
        port = bench.cfg["devices"]["navicore"].get("port")
        same = " (same USB serial)" if ((ckpt.data.get("devices") or {}).get("navicore") or {}).get("serial") else ""
        want = set(bench.wcb_numbers())
        missing, last, answered = want, None, False
        while True:
            _abort(should_abort)
            try:
                missing = want - NaviCore(bench.dev("navicore")).online_ids()
                answered = True
            except Exception as e:  # noqa: BLE001 - a held or vanished port (SerialException) blocks, never escapes
                last = e
                if not isinstance(e, AssertionError):    # the port did not open: nothing is cached, but stay symmetric
                    bench.close_device("navicore", release=False)
            if not missing:
                log("mesh: NaviCore sees every bench WCB online")
                break
            if time.monotonic() >= deadline:
                if not answered:
                    # Step 3 confirmed NaviCore by its USB serial alone, so this is the first traffic it gets: a
                    # silent NaviCore is NaviCore's problem, not the mesh's.
                    raise ResumeBlocked(
                        f"navicore on {port}{same} did not answer GET_WCB_STATUS in {timeout} s"
                        + (f" ({_first(last)})" if last else "") + " - close the Arduino IDE monitor / NaviCore tool "
                        "tab holding it, or power-cycle it (a cold boot can leave it in ROM download mode)")
                raise ResumeBlocked(f"NaviCore does not see {', '.join(f'WCB{w}' for w in sorted(missing))} online after "
                                    f"{timeout} s - powered? same channel?{hint}" + (f" ({_first(last)})" if last else ""))
            time.sleep(3)
    time.sleep(MESH_SETTLE_S)


def clean_probe(bench, name, log, should_abort=None):
    """RESET the probe (bench.probe does, retried while it boots). A probe still joined to the mesh - a cut-off
    probe_in_mesh test with the bench kept powered - is taken out: RESET does not leave the mesh, and the next MESH
    JOIN would fail with 'ERR already joined'. Returns the probe's firmware version."""
    port = bench.cfg["devices"][name].get("port")
    deadline = time.monotonic() + PROBE_WAIT_S
    while True:
        _abort(should_abort)
        try:
            p = bench.probe(name)
            break
        except Exception as e:  # noqa: BLE001
            bench.close_device(name, release=False)
            if "the harness needs" in str(e) or time.monotonic() >= deadline:
                raise ResumeBlocked(f"{name} on {port}: {_first(e)}")
            time.sleep(1.0)
    if p.mesh_id:
        mid = p.mesh_id
        log(f"{name} is still joined to the mesh as {mid} - leaving, then ?WDP,FORGET,{mid} on every WCB")
        try:
            p.mesh_leave()
        except ExpectTimeout as e:
            raise ResumeBlocked(f"{name} did not leave the mesh ({_first(e)}) - power-cycle it")
        bench.links.forget_probe(name)
        if bench.has("wcb1"):
            w = WCB(bench.dev("wcb1"))
            w.run(f"?WDP,FORGET,{mid}")
            for n in bench.wcb_numbers():
                if n != bench.usb_wcb_number():
                    w.send(f";W{n},?WDP,FORGET,{mid}")
            time.sleep(1.0)
        bench.close_device(name, release=False)
        p = bench.probe(name)
    return p.version


def check_wires(bench, ckpt, log, should_abort=None):
    """Every wire verified at the pause (or now) is checked again at its port's configured baud in its recorded
    orientation. Nothing is rediscovered. A wire that passes is marked verified - check() is the same proof verify()
    records - and links.json is saved once; nothing else in it changes. All failures are gathered into one block."""
    was = set((ckpt.data.get("links") or {}).get("verified") or [])
    bad, confirmed = [], False
    for link in bench.links.all():
        _abort(should_abort)
        if bench.links.device_only(link.wcb, link.port):
            continue
        if link.key not in was and not link.verified:
            log(f"{link.key}: not verified at the pause - not checked")
            continue
        try:
            baud = bench.port_baud(link.wcb, link.port)
        except Exception:  # noqa: BLE001
            baud = "its configured"
        if bench.links.check(link):
            log(f"{link.key} -> {link.probe_name} {link.header}: ok at {baud} baud")
            if not link.verified:      # the same stimulus, configured baud and orientation verify() records
                link.verified = True
                confirmed = True
            continue
        other = bench.links.check(link, swap=not link.swap)
        bad.append(f"{link.key} -> {link.probe_name} header {link.header} did not verify at {baud} baud. Check that "
                   f"cable: GND, and TX/RX to {link.probe_name} header {link.header}."
                   + (" (It answers with TX/RX the other way round - a different cable?)" if other else ""))
    if confirmed:
        # begin_segment records links.verified from these flags; without this a restored (or cleared) wire would
        # drop out of every later resume's check.
        bench.links.save()
    if bad:
        raise ResumeBlocked("\n".join(bad) + "\nThen resume again.")


def check_controllers(bench, ckpt, log, should_abort=None):
    """NaviCore PING (10 s) and the SBUS controller's JSON ping (60 s, it may reset on open). {name: version}."""
    out = {}
    recorded = ckpt.data.get("devices") or {}
    if bench.has("navicore"):
        port = bench.cfg["devices"]["navicore"].get("port")
        deadline, last = time.monotonic() + NAVICORE_WAIT_S, None
        while True:
            _abort(should_abort)
            try:
                out["navicore"] = NaviCore(bench.dev("navicore")).ping()
                log(f"navicore on {port}: firmware {out['navicore']}")
                break
            except Exception as e:  # noqa: BLE001
                last = e
            if time.monotonic() >= deadline:
                raise ResumeBlocked(f"navicore on {port} did not answer PING in {NAVICORE_WAIT_S} s ({_first(last)})")
            time.sleep(1.0)
    if bench.has("sbus"):
        port = bench.cfg["devices"]["sbus"].get("port")
        same = " same USB serial," if (recorded.get("sbus") or {}).get("serial") else ""
        deadline, last = time.monotonic() + SBUS_WAIT_S, None
        while True:
            _abort(should_abort)
            try:
                d = bench.dev("sbus")
                m = d.mark()
                d.send('{"t":"ping"}')
                got = d.expect(r'^\{"t":"pong".*"fwver":"([^"]+)"', timeout=3, since=m)
                out["sbus"] = got.group(1)
                log(f"sbus on {port}: firmware {out['sbus']}")
                break
            except Exception as e:  # noqa: BLE001
                last = e
            if time.monotonic() >= deadline:
                raise ResumeBlocked(f"SBUS on {port},{same} did not answer pong in {SBUS_WAIT_S} s ({_first(last)})")
            time.sleep(0.5)
        if (ckpt.in_flight or "").startswith("sbus."):
            _release_sbus(d, port, log, ckpt.in_flight)
    return out


def _release_sbus(dev, port, log, tid):
    """A cut-off sbus.* test skipped its finally. The controller keeps a held stick, button or button-mode trim in RAM
    with no timeout (SBUSController.ino "a"/"btn"/"tr" handlers), and opening its port does not reset it, so the
    re-run test would start displaced and record a false FAIL. Release them - the rest values the controller's boot
    sets - with no reset. Switches and sliders are left alone: switch_exact and slider_exact take the current pos/pct
    as their baseline. Every line starts with '{'. The cfg reply carries the controller's WiFi passwords (wifiNets),
    so its session.log line is redacted.

    Caveat for other benches: a released button writes 992 to its channel. On this bench every button and trim is on
    the matrix channel, so no switch or slider shares a channel with one."""
    def send(o):
        dev.send(json.dumps(o, separators=(",", ":")))
    old_log = dev.log
    if old_log:
        dev.log = lambda name, direction, text: old_log(name, direction, ck.redact_text(text))
    try:
        m = dev.mark()
        send({"t": "getcfg"})      # answered only within 5 s of a ping - the pong above was just received
        try:
            cfg = json.loads(dev.expect(r'^\{"e":"cfg"', timeout=5, since=m).string)
        except (AssertionError, ValueError) as e:
            raise ResumeBlocked(f"SBUS on {port} answered ping but not getcfg ({_first(e)}) - power-cycle it, then "
                                f"resume again")
    finally:
        dev.log = old_log
    send({"t": "a", "lx": 0, "ly": 0, "rx": 0, "ry": 0})
    for i, _ in enumerate(cfg.get("btn") or []):
        send({"t": "btn", "i": i, "p": False})
    for i, tr in enumerate(cfg.get("tr") or []):
        if tr.get("m") == 1:
            send({"t": "tr", "i": i, "d": 1, "p": False})
    log(f"SBUS controller: sticks centred, buttons and button-mode trims released after the cut-off test {tid}")


def _firmware_diffs(prev, cur):
    return [f"{name}: {prev[name]} -> {v} - results before and after the pause come from different images"
            for name, v in sorted(cur.items()) if v and prev.get(name) and prev[name] != v]


def _config_diffs(bench, ckpt):
    """([W<n>: missing [...] / extra [...]], {wcb: current redacted tokens}) against config_ref, or (None, {}) when
    the run has no reference."""
    ref = ckpt.data.get("config_ref") or {}
    tokens = ref.get("tokens") or {}
    if not tokens:
        return None, {}
    diffs, current = [], {}
    for w, before in sorted(tokens.items()):
        if before is None:
            continue
        try:
            raw = read_config(bench, int(w))
        except Exception as e:  # noqa: BLE001
            raise ResumeBlocked(f"W{w}'s saved config could not be read ({_first(e)})")
        bench.cache[int(w)] = raw
        now = ck.redact_tokens(raw)
        current[w] = now
        if now != before:
            missing = [t for t in before if t not in now]
            extra = [t for t in now if t not in before]
            diffs.append(f"W{w}: missing {missing} / extra {extra}")
    return diffs, current


# ---------------------------------------------------------------------------- the whole sequence
def check_bench(bench, ckpt, ask, log=None, should_abort=None, cut_off=False, on_bench_changed=None):
    """Steps 1-10 above, in order. Returns (changes accepted, firmware versions seen)."""
    log = log or bench.note
    changes, firmware = [], {}
    auto = ask is None
    log(f"===== RESUME CHECKS for {ckpt.name}" + (" (automatic, after a host outage)" if auto else ""))

    # 1. offline
    soft, hard, notes, restore = offline_differences(bench, ckpt)
    for n in notes:
        log(n)
    if hard:
        raise ResumeBlocked("\n".join(hard))
    if soft:
        for s in soft:
            log("difference: " + s)
        lines = soft + ([f"The run was cut off during {ckpt.in_flight}; it runs again first."] if ckpt.in_flight else [])
        _confirm(ask, "The bench changed since the run was paused", lines + ["", "Resume anyway?"])
        changes += [s + " (accepted)" for s in soft]
    _abort(should_abort)

    # 2. reload
    bench.close_ports(release=False)
    bench.reload_config()
    if restore:
        bench.links.restore(ckpt.data["links"]["canon"])
        log(f"links.json restored from the checkpoint ({len(bench.links.all())} wire(s))")
    else:
        bench.links.load()
    bench.invalidate()

    # 3. identity
    moves, versions = reidentify(bench, ckpt, log, should_abort, ask)
    changes += moves
    if moves and on_bench_changed:
        on_bench_changed()
    _abort(should_abort)

    # 4. a cut-off test's leftovers
    if ckpt.in_flight or cut_off:
        tid = ckpt.in_flight or "the last test"
        text = (f"The run was cut off during {tid}, so its cleanup never ran: RAM-only state (?DEBUG, ?RTERM, ;P pins, "
                f"NaviCore debug flags) may still be on. Reboot every WCB and zero NaviCore's debug flags before "
                f"continuing? (Recommended.)")
        if auto or ask("Reboot the WCBs first?", text, True):
            reboot_wcbs(bench, log, ckpt)
            changes.append(f"WCBs rebooted after the cut-off test {tid}")
        else:
            log("the reboot after the cut-off test was declined")
    _abort(should_abort)

    # 5. mesh
    wait_mesh(bench, ckpt, log, should_abort)

    # 6. firmware of the WCBs and probes identified in step 3
    segs = ckpt.data["segments"]
    prev = (segs[-1].get("firmware") if segs else None) or {}
    fw = _firmware_diffs(prev, versions)
    if fw:
        _confirm(ask, "Firmware changed since the pause", fw + ["", "Resume anyway?"])
        changes += fw
    firmware.update({k: v for k, v in versions.items() if v})
    _abort(should_abort)

    # 7. saved config
    diffs, current = _config_diffs(bench, ckpt)
    if diffs is None:
        log("no saved-config reference for this run (a run under 5 tests is not snapshotted at its start, and it was "
            "not paused cleanly) - the saved-config comparison is skipped")
    elif diffs:
        ref = ckpt.data["config_ref"]
        where = f"the reference taken {ref.get('at')} ({ref.get('taken')}" + (
            f", after {ref['after_test']}" if ref.get("after_test") else "") + ")"
        why = (f"The cut-off test {ckpt.in_flight} probably left it." if ckpt.in_flight
               else "Something changed the saved config while the run was paused.")
        _confirm(ask, "Saved config differs", [f"The saved config differs from {where}:"] + diffs +
                 [why, "", "Yes = continue; the boards keep this config. No = stay paused and fix it by hand."])
        ckpt.set_config_ref(current, "accepted")
        changes.append("config accepted with difference: " + "; ".join(diffs))
    else:
        log("saved config matches the run's reference")
    _abort(should_abort)

    # 8. probes
    for name in bench.probe_names():
        _abort(should_abort)
        firmware[name] = clean_probe(bench, name, log, should_abort)

    # 9. wires
    check_wires(bench, ckpt, log, should_abort)

    # 10. controllers
    ctl = check_controllers(bench, ckpt, log, should_abort)
    fw = _firmware_diffs(prev, ctl)
    if fw:
        _confirm(ask, "Firmware changed since the pause", fw + ["", "Resume anyway?"])
        changes += fw
    firmware.update(ctl)

    log("RESUME CHECKS OK" + (f" - {'; '.join(changes)}" if changes else ""))
    return changes, firmware
