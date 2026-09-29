"""The two tools through Intellex on the bench boards (docs/hil_plan/INTELLEX.md IX-WP7 and IX-WP8; docs/HIL_TESTING.md
§10): the WCB Wizard on W1 (and W2 over the mesh), and the NaviCore config tool on NaviCore's own COM port and through
W1 (finding 4).

Each test stages Intellex with this repo's Wizard and NaviCore's config tool (tools="worktree"), starts a leashed host
allowed that one COM port, attaches it there, and runs the Playwright spec of the same id (tests/intellex/specs/
wizard_board.spec.js, nc_board.spec.js). The page connects on its own through the shim - no port picker, no Chrome
profile - and the spec reaches the rest of the bench through the harness bridge (hil/intellex.py run_intellex_test).
The harness keeps every other port and owns the verdict on the bench:
  - rawLink: a /_link client of the harness's own reads every byte the host fans out for the whole run, and the test
    fails when the board printed a boot line, or - through W1's console - W1 heard another board boot (boot_check).
    Intellex's promise is that opening, reloading and closing a tool resets nothing.
  - off the stream (DX20): W2 prints '[ETM] WCB1 came ONLINE (boot)' for each boot announce of W1's it hears, W1 the same
    for NaviCore (WCB 20), and NaviCore's uptime (GET_MESH_STATS upMs) keeps counting.
  - config_guard on W1 and every other WCB while the Wizard runs (the shim pulls each one W1 hears); nc_guard only where
    a test saves NaviCore's config. The NaviCore tool's connect writes nothing to NaviCore's flash - it compares the
    command library and never syncs it (config_tool/index.html:9139-9155, :14487-14496) - so the other NaviCore specs
    fail on any JSON the tool sends that is not a read (tests/intellex/lib/board.js NC_READ_ONLY) instead.
  - afterwards: ?RTERM,STOP on every other WCB (the shim's mesh routing arms their remote terminals through W1,
    setRemoteConnected -> startRemoteTermSession, Wizard/app.js:6368-6429), and STOP_MONITOR with debug flags 0 on
    NaviCore whenever a NaviCore tool ran (its connect starts both, RAM only, NaviCore.ino:489-494).
  - a W1 that does not answer once the host has let it go is reset into its app (hil/intellex.py reset_into_app), and
    the test fails naming it: through Intellex the Wizard holds W1's GPIO0 low (finding 16).
A spec is handed lengths, versions, keys, labels, bauds and non-secret config fields only: W1's ?backup and every pull
carry the mesh password, NaviCore's GET_CONFIG the AP password as well. The CONFIG line's byte comparison is done here,
on the rawLink bytes, and never quoted.

No test here can move a servo (hil/servos.py): the Wizard only pulls, pushes one label and types a ;S1 line; the
NaviCore tool reads, starts the monitor and sets debug flags, and nc_save_unchanged saves chRateHz, the rc_ch monitor
rate - applyConfigSideEffects re-opens no port whose baud is unchanged and re-applies easing only where it changed
(NaviCore.ino:3236-3272, :3321-3345). W1's restart in wizard_reboot_w1_boots_app writes nothing to a Maestro.
"""
import json
import time

from hil.intellex import (NAVICORE_BOOT_MARKERS, ROM_LOADER_MARKERS, WCB_BOOT_MARKERS, WCB_READY, boot_check,
                          line_spans, reset_into_app, run_intellex_test)
from hil.navicore import NaviCore
from hil.nc_guard import nc_guard
from hil.runner import Skip, test
from hil.wcb import WCB, group_tokens
from suites.common import config_guard, link, marker, remote_wcbs, snapshot, token, usb_wcb
from suites.s03_wcb import _clear, _factory_reply, _grow_over
from suites.s33_intellex_bench import _boot_edges
from suites.s99_etm import _peers_online

NAVICORE_ID = 20                  # the special peer; a NaviCore reports its own id in GET_WCB_STATUS "self"
CONFIG_TAG = b'{"type":"CONFIG","data":'
RELAY_WINDOW_S = 21.0             # W1 relays mesh JSON to USB for 20 s after a ;W20,{...} line (WCB.ino:8019-8021)

# Non-secret scalar fields of NaviCore's GET_CONFIG a spec may be handed (rc_config.h:1221-1234, the wcbNetwork block).
# Never wifiSsid, wifiPassword or the mesh password; the whole line is compared here, byte for byte, on the rawLink.
NC_FIELDS = ("txModel", "threeAxisGimbals", "sbusOutEnabled", "wifiEnabled", "maeGateMs", "boardType", "tapWindowMs",
             "holdMs", "switchSettleMs", "chRateHz", "matrixChannel", "matrixDebounceFrames", "wcbNetwork.deviceId",
             "wcbNetwork.channel")


def _guarded(bench):
    """W1 and every other WCB: through Intellex the Wizard's shim pulls each board W1 hears (routeMeshThroughBoard)."""
    return [bench.usb_wcb_number()] + remote_wcbs(bench)


def _stop_rterm(bench):
    """?RTERM,STOP on every WCB but W1 - on its own USB, or relayed from W1 - so no remote terminal the shim armed
    through W1 outlives the test (RAM only; the F21 precedent: a live session slows the suites that follow)."""
    usb = bench.usb_wcbs()
    for n in remote_wcbs(bench):
        try:
            if n in usb:
                WCB(bench.dev(usb[n])).run("?RTERM,STOP")
            else:
                usb_wcb(bench).send(f";W{n},?RTERM,STOP")
        except AssertionError as e:
            bench.note(f"?RTERM,STOP on WCB{n} failed: {(str(e).splitlines() or [repr(e)])[0]}")


def _nc_quiet(bench):
    """NaviCore's monitor stopped and its debug flags back to 0, the harness's baseline (nc_guard's _quiet): the tool's
    connect sets both, RAM only. Nothing on a bench without NaviCore."""
    if not bench.has("navicore"):
        return
    try:
        nc = NaviCore(bench.dev("navicore"))
        nc.ack({"type": "STOP_MONITOR"})
        nc.set_debug_flags(0)
    except AssertionError as e:
        bench.note(f"NaviCore's monitor / debug reset failed: {(str(e).splitlines() or [repr(e)])[0]}")


def _on_w1(bench, test_id, args=None, restarts=False, timeout=300.0):
    """Run spec `test_id` against a leashed Intellex host attached to W1's COM port. The spec gets com, wcb (W1's
    number, the relay slot), boards (the other WCBs), clients (NaviCore's id when the bench has one: W1 hears it as a
    client) and navicore, plus `args`. Unless restarts=True (W1 restarts during the test by design): the rawLink check
    fails the test on any boot line of W1's and on any board W1 hears booting, and W2's console must not have heard W1
    boot. Every other WCB's remote terminal is stopped afterwards, whatever happened."""
    w1n = bench.usb_wcb_number()
    port = bench.cfg["devices"]["wcb1"]["port"]
    others = remote_wcbs(bench)
    nav = bench.has("navicore")
    w2 = WCB(bench.dev("wcb2")) if bench.has("wcb2") and not restarts else None
    m2 = w2.dev.mark() if w2 else None
    base = {"com": port, "wcb": w1n, "relay": w1n, "boards": others, "clients": [NAVICORE_ID] if nav else [],
            "navicore": nav}
    check = None if restarts else boot_check(WCB_BOOT_MARKERS, "W1", boots_of=others + ([NAVICORE_ID] if nav else []))
    try:
        run_intellex_test(bench, test_id, attach={"kind": "serial", "port": port}, device="wcb1",
                          args=dict(base, **(args or {})), link_check=check, recover=reset_into_app, timeout=timeout)
    finally:
        _stop_rterm(bench)
    if w2:
        time.sleep(2.0)                       # a boot's first announce goes ~1.5 s after its ETM block
        edges = _boot_edges(w2.dev, m2, w1n)
        assert not edges, f"W2 heard W{w1n} boot {len(edges)} time(s) during the test"


def _config_line_check(text):
    """A link_check: every CONFIG line NaviCore printed through the link carries exactly `text`, the GET_CONFIG data the
    harness read over USB just before - the whole config crossed Intellex byte for byte. Lengths only in a message."""
    want = text.encode("utf-8")

    def check(data):
        spans = line_spans(data, CONFIG_TAG)
        if not spans:
            return ["no CONFIG line came through the link: the tool's GET_CONFIG was never answered"]
        out = []
        for i, (a, b) in enumerate(spans, 1):
            line = data[a:b].rstrip(b"\r\n")
            k = line.find(CONFIG_TAG)
            body = line[k + len(CONFIG_TAG):-1] if line.endswith(b"}") else None
            if body != want:
                out.append(f"CONFIG line {i} through the link ({len(line)} bytes) does not carry the config NaviCore "
                           f"gives over USB ({len(want)} bytes of data)")
        return out
    return check


def _nc_fields(cfg):
    """NC_FIELDS that GET_CONFIG `cfg` holds as a bool or a number -> {path: value}."""
    out = {}
    for path in NC_FIELDS:
        v = cfg
        for k in path.split("."):
            v = v.get(k) if isinstance(v, dict) else None
        if isinstance(v, (bool, int, float)):
            out[path] = v
    return out


def _on_navicore(bench, test_id, args=None, link_extra=None, timeout=300.0):
    """Run spec `test_id` against a leashed Intellex host attached to NaviCore's COM port. The spec gets version (the
    PONG's, read here over USB first) and com, plus `args`. The rawLink check fails the test on NaviCore's ROM line,
    banner or reset reason (plus link_extra's problems); afterwards NaviCore's monitor and debug flags are reset, its
    uptime must have kept counting, and W1 must not have heard it boot. Skips while NaviCore's recorder holds anything,
    as every test that could restart it does (D33): a regression here would restart it."""
    nc = NaviCore(bench.dev("navicore"))
    why = nc.restart_blocker()
    if why:
        raise Skip(why)
    version = nc.ping()
    nid = nc.wcb_status()["self"]
    up0, t0 = nc.mesh_stats()["upMs"], time.monotonic()
    w1 = WCB(bench.dev("wcb1")) if bench.has("wcb1") else None
    m1 = w1.dev.mark() if w1 else None
    port = bench.cfg["devices"]["navicore"]["port"]
    boot = boot_check(NAVICORE_BOOT_MARKERS, "NaviCore")
    check = (lambda data: boot(data) + link_extra(data)) if link_extra else boot
    try:
        run_intellex_test(bench, test_id, attach={"kind": "serial", "port": port}, device="navicore",
                          args=dict({"version": version, "com": port}, **(args or {})), link_check=check,
                          timeout=timeout)
    finally:
        _nc_quiet(bench)
    nc = NaviCore(bench.dev("navicore"))
    up1, gone = nc.mesh_stats()["upMs"], (time.monotonic() - t0) * 1000
    problems = []
    if up1 < up0 + gone - 3000:
        problems.append(f"NaviCore's uptime went from {up0} to {up1} ms across {gone:.0f} ms: it restarted")
    if w1:
        edges = _boot_edges(w1.dev, m1, nid)
        if edges:
            problems.append(f"W1 heard WCB{nid} boot {len(edges)} time(s)")
    assert not problems, "; ".join(problems)


# ============================================================================================ IX-WP7: the Wizard on W1
@test("intellex.wizard_pull_w1", "Through Intellex attached to W1's COM port the Wizard connects on its own and pulls "
      "W1 - no click, a baseline within 45 s - and shows its saved bauds and labels, with the chip 'Intellex · USB "
      "<port> ▾'; W1 never restarted (IX-WP7)", needs=["wcb1"])
def wizard_pull_w1(bench):
    """A port of wizard.pull (s30_wizard.py) to Intellex's auto-connect: intellex_shim.js autoConnectWcb calls the
    Wizard's own _modalDoConnect(1, port), which pulls 3 s later (Wizard/app.js:6336-6355). Only the ?BAUD and ?LABEL
    tokens go to the spec."""
    with config_guard(bench, *_guarded(bench)) as before:
        toks = [t for t in before[bench.usb_wcb_number()] if t.upper().startswith(("?BAUD,S", "?LABEL,S"))]
        _on_w1(bench, "intellex.wizard_pull_w1", args={"tokens": toks})


@test("intellex.wizard_push_label_w1", "A label typed into the Wizard and pushed through Intellex lands in W1's saved "
      "config, and is put back (IX-WP7)", needs=["wcb1"])
def wizard_push_label_w1(bench):
    """A port of wizard.push_label (s30_wizard.py). The spec waits for the shim's own pulls of the other boards first, so
    the push is the only thing on W1's stream."""
    w1n = bench.usb_wcb_number()
    with config_guard(bench, *_guarded(bench)) as before:
        t = marker()
        try:
            _on_w1(bench, "intellex.wizard_push_label_w1", args={"port": 5, "label": t})
            assert f"?LABEL,S5,{t}" in snapshot(bench, w1n), "the label pushed through Intellex is not in W1's ?backup"
        finally:
            orig = token(before[w1n], "?LABEL,S5,")
            usb_wcb(bench).run(orig if orig else "?LABEL,CLEAR,S5")


@test("intellex.wizard_terminal_wire", "A ;S1 line typed into the Wizard's terminal through Intellex comes out of W1 S1 "
      "exactly once: the CR framing through the shim, proved on the wire (IX-WP7)", needs=["wcb1", "probe1"])
def wizard_terminal_wire(bench):
    """A port of wizard.terminal_wire. The wire is bound before the host takes W1: binding reads W1's configured baud."""
    link(bench, bench.usb_wcb_number(), "S1").listen()
    with config_guard(bench, *_guarded(bench)):
        _on_w1(bench, "intellex.wizard_terminal_wire", args={"port": "S1"})


@test("intellex.wizard_mesh_autopull_w2", "Through Intellex on W1, the shim routes the mesh through W1 with no click: "
      "W2 is armed and pulled once, W1 its relay, a baseline naming WCB 2; NaviCore (a client) and nothing else is ever "
      "pulled, and nothing again over two more sweeps (IX-WP7)", needs=["wcb1", "wcb2"])
def wizard_mesh_autopull_w2(bench):
    """intellex_shim.js:1197-1316 routeMeshThroughBoard: sequential remoteBoardPull(slot, n, 1, 3, cb) per board the
    Wizard's WDP sweep lists without CLIENT=1, once per board (_meshRoutedOnce). The relay slot is compared as a string
    here; its type is finding 17's (should) test."""
    with config_guard(bench, *_guarded(bench)):
        _on_w1(bench, "intellex.wizard_mesh_autopull_w2", timeout=360)


@test("intellex.wizard_mesh_relay_slot_number", "(should) A board the shim routes through W1 is filed under relay slot "
      "1 as a number, as the Wizard's own callers file it, so a W1 link drop clears and re-arms it (IX-WP7, INTELLEX.md "
      "finding 17)", needs=["wcb1", "wcb2"])
def wizard_mesh_relay_slot_number(bench):
    """The shim passes _wdpMeshConn().slot, a string key of Object.entries (Wizard/app.js:13420-13428), to
    setRemoteConnected (intellex_shim.js:1264); every relay-slot comparison in the Wizard is === against a number
    (app.js:5790, :5939-5942, :6432-6436)."""
    with config_guard(bench, *_guarded(bench)):
        _on_w1(bench, "intellex.wizard_mesh_relay_slot_number")


@test("intellex.wizard_remote_pull_parts", "Through Intellex on W1, the shim pulls W2's config of over 2912 characters "
      "as [MGMT:CFGPART,2] parts across the Intellex link, and the Wizard joins and checks them: onComplete once with "
      "true, every sequence whole, no raw part text in a terminal (IX-WP7)", needs=["wcb1", "wcb2"])
def wizard_remote_pull_parts(bench):
    """A port of wizard.remote_pull_parts (s30_wizard.py _remote_pull), except that the pull is the shim's own: W2 is
    grown on its own USB first, then the Wizard is only opened. The spec gets W2's version, the throwaway keys with their
    value lengths and the reply length - never a token: the chain carries the mesh password and the WiFi passphrase."""
    n2 = bench.cfg["devices"]["wcb2"]["wcb"]
    w2 = WCB(bench.dev("wcb2"))
    keys = []
    with config_guard(bench, *_guarded(bench)):
        try:
            ver = w2.version()
            _grow_over(w2, ver, keys)
            chain = _factory_reply(w2, ver)
            seqs = {k: len(t) - len(f"?SEQ,SAVE,{k},") for k in keys
                    for t in group_tokens(chain[chain.index("]") + 1:].split("^")) if t.startswith(f"?SEQ,SAVE,{k},")}
            assert sorted(seqs) == sorted(keys), f"W2's ?backup lacks {sorted(set(keys) - set(seqs))}"
            bench.note(f"intellex.wizard_remote_pull_parts: W2's reply is {len(chain)} characters, throwaway sequences "
                       f"{seqs}")
            _on_w1(bench, "intellex.wizard_remote_pull_parts", timeout=360,
                   args={"target": n2, "ver": ver, "seqs": seqs, "length": len(chain), "minParts": 2})
        finally:
            _clear(w2, keys)


@test("intellex.wizard_reload_no_reset_w1", "Three F5 reloads of the Wizard through Intellex on W1: the link comes back "
      "each time, W1 is pulled into slot 1 again, and it never restarted (IX-WP7)", needs=["wcb1"])
def wizard_reload_no_reset_w1(bench):
    """The host keeps W1's port open across page loads (Intellex src/host.py: pages bind and unbind, the transport
    stays), and the shim turns F5 into location.reload() (intellex_shim.js:375-380)."""
    with config_guard(bench, *_guarded(bench)):
        _on_w1(bench, "intellex.wizard_reload_no_reset_w1")


@test("intellex.shell_split_w1", "The split shell through Intellex on W1: both tools connect over the one link - the "
      "Wizard pulls W1, the NaviCore pane reaches NaviCore through W1 (or waits, without one) - with the link labels, "
      "and nothing restarted (IX-WP7)", needs=["wcb1"])
def shell_split_w1(bench):
    """Intellex src/shell.html: /_shell?view=split loads both tools as frames of one page, each with its own /_link on
    the host's one transport. The NaviCore pane's transport there is recorded, and judged by nc_via_usb_doorway."""
    with config_guard(bench, *_guarded(bench)):
        try:
            _on_w1(bench, "intellex.shell_split_w1")
        finally:
            _nc_quiet(bench)


@test("intellex.wizard_reboot_w1_boots_app", "(should) W1 restarted with ?reboot while the Wizard holds it through "
      "Intellex - whose connect asserted DTR, holding W1's GPIO0 low - boots its app, not the ESP32 ROM loader (1 W1 "
      "restart; the harness resets W1 into its app if not; IX-WP7, INTELLEX.md finding 16)", needs=["wcb1"])
def wizard_reboot_w1_boots_app(bench):
    """Wizard/app.js:5364 asserts DTR alone on every connect; through Intellex it reaches W1's CH9102 (intellex_shim.js
    :248-260 -> host.py:1239-1260 -> serial_transport.py set_signals), where the auto-reset circuit drives GPIO0 from
    DTR (docs/HIL_TESTING.md §2), and Intellex opened the port with both lines low (serial_transport.py:65-68). Ends with
    every peer online again, as every W1 restart does."""
    try:
        with config_guard(bench, *_guarded(bench)):
            _on_w1(bench, "intellex.wizard_reboot_w1_boots_app", restarts=True,
                   args={"ready": WCB_READY, "rom": list(ROM_LOADER_MARKERS)})
    finally:
        try:
            _peers_online(bench, usb_wcb(bench), strict=False)
        except AssertionError as e:
            bench.note(f"after W1's restart the peer check failed: {(str(e).splitlines() or [repr(e)])[0]}")


@test("intellex.nc_via_usb_doorway", "(should) The NaviCore config tool reached through W1 over USB ends up in Via WCB "
      "- the cold connect and a reload right after - so 'Update over USB (OTA)' cannot send ?OTALOCAL to W1 (IX-WP7, "
      "INTELLEX.md finding 4)", needs=["wcb1", "navicore"])
def nc_via_usb_doorway(bench):
    """A serial attach reports role "" (Intellex src/host.py:1117-1133), so the shim never forces Via WCB
    (intellex_shim.js:440-444), and the tool takes any PONG (index.html:9515-9519). W1 prints NaviCore's reply to a
    bare, broadcast PING (rc_telemetry.h:2186-2192) only inside the 20 s relay window a ;w20,{...} line opens
    (WCB.ino:8019-8021, :5513): the reload lands inside it. The companion data - whether the direct PING was answered -
    is in session.log; NaviCore's monitor and debug flags are reset afterwards.

    RELAY_WINDOW_S first, with nothing attached to W1, so the first connect is a cold one: a test just before this one
    (shell_split_w1, a navicore.* test's ;W20,{...}) leaves the window open, and the cold connect then takes the mirrored
    PONG too - seen in the dry run."""
    time.sleep(RELAY_WINDOW_S)
    try:
        _on_w1(bench, "intellex.nc_via_usb_doorway")
    finally:
        _nc_quiet(bench)


# ============================================================================================ IX-WP8: the NaviCore tool
@test("intellex.nc_autoconnect", "Through Intellex on NaviCore's COM port the config tool connects on its own and "
      "directly: 'Connected ✓', the label '· USB <port> ▾', the PONG's version, the flash buttons wired to the host with "
      "their native titles, OTA 'Update over USB (OTA)', only reads sent; NaviCore never restarted (IX-WP8)",
      needs=["navicore"])
def nc_autoconnect(bench):
    """intellex_shim.js:396-450 autoConnect -> connectDirect -> openPortAndStart (index.html:4511-4605);
    wireNativeFlash and relabelOtaButton (:574-666)."""
    _on_navicore(bench, "intellex.nc_autoconnect")


@test("intellex.nc_reload_no_reset", "Three F5 reloads of the config tool through Intellex on NaviCore's COM port: "
      "each reconnects directly with the same PONG version, and NaviCore never restarted - no banner on the link, its "
      "uptime unbroken, no boot announce heard by W1 (IX-WP8)", needs=["navicore"])
def nc_reload_no_reset(bench):
    """The reason Intellex exists (Intellex CLAUDE.md, 'Why native serial'): Chrome asserts DTR and RTS in Web Serial's
    open() and NaviCore's USB-Serial/JTAG resets it on every page load; the host keeps the port open across loads."""
    _on_navicore(bench, "intellex.nc_reload_no_reset")


@test("intellex.nc_config_matches", "The config the tool loads through Intellex is NaviCore's: every CONFIG line on the "
      "link is byte-identical to GET_CONFIG over USB, named non-secret fields match in the tool's config and baseline, "
      "and a Save right after would send nothing (IX-WP8)", needs=["navicore"])
def nc_config_matches(bench):
    """GET_CONFIG is one line of about 14 KB holding the mesh and AP passwords (hil/navicore.py config): it is compared
    here on the rawLink bytes, and the spec gets only NC_FIELDS' values."""
    text = NaviCore(bench.dev("navicore")).config(raw=True)
    fields = _nc_fields(json.loads(text))
    _on_navicore(bench, "intellex.nc_config_matches", args={"fields": fields}, link_extra=_config_line_check(text))


@test("intellex.nc_setsignals", "The config tool's setSignals(DTR false, RTS false) through Intellex reaches NaviCore's "
      "port - 200 from the host, on connect and again from the page - and resets nothing: NaviCore answers a PING on "
      "the same link after (IX-WP8)", needs=["navicore"])
def nc_setsignals(bench):
    """index.html:4528-4540 (the deassert after open) -> intellex_shim.js:248-260 -> host.py:1239-1260 ->
    serial_transport.py set_signals. On NaviCore's native USB, DTR=0 with RTS=0 holds nothing in reset."""
    _on_navicore(bench, "intellex.nc_setsignals")


@test("intellex.nc_save_unchanged", "Save in the config tool through Intellex: with no edit nothing is sent; chRateHz "
      "saved one step away and back is ACKed both times as a one-branch SET_CONFIG, and NaviCore's config ends "
      "byte-identical (opt-in intellex_nc_save; IX-WP8)", needs=["navicore"], opt_in="intellex_nc_save")
def nc_save_unchanged(bench):
    """Inside nc_guard, which proves the config byte-identical afterwards (and restores it if not). The plan's 'Save with
    no edits (SET_CONFIG)' sends nothing (saveConfigToBoard's no-diff return, index.html:16911-16925), so the round trip
    is made with one field; both saves rewrite /config.json on LittleFS."""
    with nc_guard(bench) as g:
        try:
            _on_navicore(bench, "intellex.nc_save_unchanged")
        finally:
            g.nc = NaviCore(bench.dev("navicore"))      # the guard restores through the port as it is now
