"""The NaviCore config tool (NaviCore/config_tool/index.html), ids nctool.* (docs/hil_plan/NAVICORE.md §5, NC-WP3 and
NC-WP13). Each test runs the Playwright spec of the same id in tests/wizard/specs/navicore, or the node tests of
tests/wizard/unit/navicore, the way s30_wizard.py runs the Wizard's.

  L0  nctool.static, nctool.unit      node --test: the page's syntax, the firmware/tool constant pairs, the page's own
                                      pure functions lifted into node, and the rig's model of the firmware
  L1  nctool.<spec>                   the real page with a fake navigator.serial backed by an in-Node NaviCore
                                      emulator; no board, no port, nothing on the bench changes (device=None)
  L2  nctool.board_*                  the same fake port piped to the REAL NaviCore through the harness's own COM
      nctool.emulator_contract        handle (run_wizard_test(..., pipe=True)), inside nc_guard; board_via_wcb and the
                                      (should) board_usb_probe_no_broadcast pipe W1's port instead
  L3  nctool.webserial_*              real Web Serial on NaviCore's own port (Chrome's grant in .profiles/navicore) and
                                      esptool-js, inside nc_guard: attended, opt-in navicore_webserial

(should) specs assert what the tool ought to do and fail until it does (docs/HIL_TESTING.md §6); in Playwright they are
test.fail(), so a standalone or CI run stays green and turns red the day the fix lands. They need the NaviCore repo beside
this one (tests/wizard/lib/navicore/paths.js); without it every nctool test skips.

What an L2/L3 spec must not work out for itself comes in run_wizard_test's args: a free button slot, a marker, a label,
the bench image's path, what the harness read of the board (counts, names, a hash; never a config value). NaviCore's
flash holds Greg's own clips and command library: these tests list them, never write them. Line numbers are NaviCore's
hil-week tree (6925773, the bench image) and this repo's Code/WCB.
"""
import json
import os
import time

from hil import ncflash
from hil.checkpoint import redacted_diff
from hil.nc_guard import config_diff, nc_guard, read_config
from hil.navicore import NaviCore, fnv1a32
from hil.runner import Skip, test
from hil.wizard import run_unit_tests, run_wizard_test
from suites.common import link, marker, padded, usb_wcb, usb_wcb_number
from suites.s21_navicore_sbus import _sbus_setup
from suites.s42_navicore_sbus_engine import _check_value, _stick, axis_arg, axis_value
from suites.s47_navicore_ota import (_bench_image, _flash_pass, _idle, _line1, _on_bench_image, _record, _restartable,
                                     _runs, _slot, _status)


@test("nctool.static", "The config tool checked without running it: every inline script compiles, flasher.js and "
      "serial-hub.js pass node --check, the firmware/tool constant pairs agree, every key GET_CONFIG prints is read by "
      "applyConfig, Intellex ships the same bytes, the shared-hub protocol matches the Wizard's, and what the Intellex "
      "shim drives exists (L0, no board)")
def static(bench):
    run_unit_tests(bench, files=("unit/navicore/static.test.js",))


@test("nctool.unit", "The config tool's pure functions in node: the save diff and its null branches (D-NC22), the bridge "
      "fragmenter over a hostile corpus, every command-library command through the codec, the sequence renderer against "
      "the Wizard's, parseCsv and the import sniffer; plus the rig's own model of the firmware (L0, no board)")
def unit(bench):
    run_unit_tests(bench, files=("unit/navicore/unit.test.js", "unit/navicore/rig.test.js"))


# L1: the real page, a fake port, the emulator. (key, what the spec proves)
_L1 = (
    ("load_smoke", "GET /NaviCore/ lands on the tool, which loads its command library (wcb-native hidden) and opens its modals with no page error"),
    ("connect_direct_sequence", "Connect via USB opens at 115200, drops DTR/RTS before any write, and handshakes PING, GET_CONFIG, GET_CMDLIB_META, START_MONITOR, SET_DEBUG_FLAGS (all sys:1); the debug chips drive the flags"),
    ("pong_epoch_slow_direct", "(should) A direct board whose PONG comes 3.5 s late is still taken for a direct link, not 'switched to Via WCB'"),
    ("transport_flag_reset", "Every connect and disconnect puts the firmware buttons on the live transport (Via WCB -> Disconnect -> USB)"),
    ("disconnect_teardown", "Disconnect stops the monitor, releases and closes the port, drops the baseline and says a pending save was not confirmed"),
    ("link_loss_reconnect", "A lost device (NetworkError) tears down and auto-reconnects to the one granted port; a BreakError keeps the session; pagehide drops DTR/RTS and closes"),
    ("link_loss_ambiguous_ports", "With two granted ports a lost link is torn down and not reopened"),
    ("via_wcb_hub", "A cancelled picker says 'No port selected'; picking the WCB elects the tab hub leader, handshakes with ;w20,{\"sys\":1,\"type\":\"PING\"} and shows the relay chip; DTR is never asserted"),
    ("keepalive_via_wcb", "Bridged, a silent PING goes out every 10 s and stops with the session"),
    ("via_wcb_nothing_bare", "(should) Connecting Via a WCB writes only ;w20,-wrapped lines to the WCB; today the first status poll goes out bare, before the Via-WCB flag is set, and a WCB broadcasts a bare line (D-NC71)"),
    ("doorway_pong_misdetect", "(should) A PONG relayed through a WCB is not taken for a direct link: Via WCB, and USB OTA disabled (D-NC30)"),
    ("rx_framing_markers", "Lines cut anywhere (a UTF-8 character included) reassemble; [MAE:], [OTA:], [TERM:] and plain text go where they belong; debug lines follow their chip; the pane keeps 3000 lines"),
    ("rx_fragments", "A fragmented CONFIG applies once whatever the order, duplicates and noise; broken envelopes are ignored; an idle session expires and never completes"),
    ("config_error_no_baseline", "A GET_CONFIG answered with an ERROR leaves the tool unloaded, and Save warns before pushing its defaults"),
    ("apply_all_keys", "Every key GET_CONFIG carries lands in the tool unchanged but for four documented normalizations, and the General and WCB fields show it"),
    ("no_phantom_diff", "Opening every Config tab and editor, then Save, sends nothing; the last tab is remembered and a stale name falls back to channels"),
    ("save_diff_payload", "An edit typed into General is the only thing Save sends; the hold clamp matches the firmware; a second Save sends nothing"),
    ("save_ack_correlation", "A NACK and a 12 s silence leave the baseline alone, an overlapping Save is refused, a stale saveId is ignored, an ACK with no saveId still counts"),
    ("reset_defaults_flow", "Restore Defaults asks, sends RESET_DEFAULTS then GET_CONFIG, reports success only once the CONFIG is in, warns after 12 s without one, and refuses over the bridge"),
    ("reset_defaults_needs_save", "(should) After Restore Defaults the reset stays unsaved until Save stores it: Save sends the defaults instead of answering 'No changes to save' (D-NC74)"),
    ("refresh_overwrites_edits", "(should) Refresh with unsaved edits asks first; declined, the edit stays and nothing is re-read (D-NC31)"),
    ("close_prompt", "Closing Config prompts only for edits made in that window and not yet on the board: Cancel keeps them unsaved, OK saves"),
    ("push_budget_prediction", "The Config footer predicts the bridged Save: a single packet for a small edit, and exactly as many fragments as the Save sends"),
    ("sendjson_fragments_exact", "A bridged Save of hostile text goes out in <= 187-byte envelopes, >= 100 ms apart with no poll between parts, and lands byte-exact"),
    ("push_refused_not_pending", "(should) A bridged Save over the 192-fragment cap is refused before a byte is sent and leaves no save pending"),
    ("sendline_chunking", "A SET_CONFIG over 512 bytes goes out in 512-byte pieces >= 4 ms apart, and a send made meanwhile waits for the whole line"),
    ("noop_apply_every_editor", "(should) Applying every button, switch and knob editor unchanged changes nothing, so Save sends nothing (D-NC32)"),
    ("skip_running_saved", "(should) Apply on a mapping whose action has 'skip if running' keeps the gate on the board (the tool sends skipRunning:1, which ArduinoJson reads as false)"),
    ("button_modal_trigger", "Shift-click fires TRIGGER with the modifier's tap tier; a new mapping built in the modal saves as exactly that mapping, and Clear All sends {}"),
    ("test_action_button", "A row's Test fires TEST_ACTION with the row's current values minus delay and note; a recorder control is refused locally"),
    ("test_action_refusal_shown", "(should) A TEST_ACTION the board refuses (ok:false) is reported to the user (D-NC20)"),
    ("command_view_limits", "The command field reserves the ;W<n>;S<p> prefix after a destination change, warns past 95, and flags only a chained unicast of implicitly-routed verbs"),
    ("command_view_cap_on_open", "(should) An action stored with a ;W<n>;S<p> prefix opens with the prefix already reserved in the field cap"),
    ("wcb_network_profiles", "The dirty badge tracks the WCB Network fields, a USB Save carries the branch, a profile loads into the fields, and a seventh profile is refused"),
    ("wcb_network_bridged_strip", "Over the bridge a WCB Network edit is not sent: alone it says so, with other edits it is stripped and kept out of the new baseline"),
    # The Firmware tab: GitHub, CryptoJS and esptool-js served by page.route; the emulator speaks ?OTALOCAL and the ?OTA relay.
    ("fw_flash_mocked", "Update Firmware writes boot, partitions and app from GitHub's listing (never a decoy) with otadata reset, hands esptool-js the port with no stray writes and resumes the session; Latest on GitHub reads the same listing"),
    ("fw_flash_partial_refused", "A firmware set with only one of the bootloader / partition-table pair is refused before the bootloader is touched; with neither, the flash is app-only"),
    ("fw_refused_flash_keeps_session", "(should) An Update refused before anything is written leaves the live session connected"),
    ("fw_wipe_regions", "Full Wipe asks first, then erases NVS and otadata ahead of boot, partitions and app on the granted port, and writes nothing at or above app1 (config LittleFS and clips untouched)"),
    ("fw_wipe_text", "(should) Nothing the Full Wipe shows promises the saved configuration is erased, since the flasher never writes the config LittleFS (D-NC34)"),
    ("ota_usb_state_machine", "OTA over USB survives coalesced and split markers and a lost last ACK, skips late cursor markers ahead of END,OK, locks the port while it streams, writes the image byte-exact and reconnects across the restart"),
    ("ota_usb_failures", "OTA over USB: a rejected BEGIN and a failed verify are reported as failures and ABORTed, and the board keeps its image on the live session"),
    ("ota_usb_lost_chunk", "(should) OTA over USB resends a lost DATA line once the chunks behind it are NAKed, not after the 10 s stall timeout"),
    ("ota_wcb_state_machine", "OTA over WCB: 192-B CRC-suffixed chunks, eight in flight, one session; a CRC-dropped line is resent, other targets' and sessions' ACKs are ignored, only the offset-0 END answer counts"),
    ("ota_wcb_failures", "OTA over WCB: a rejected BEGIN and an unverifiable END are reported as failures and ABORTed on the session; the target keeps its image"),
    # Record/replay clips: the emulator holds a clip store and answers ?REC (lib/navicore/clips.js).
    ("clips_list_forms", "Relayed clip lists parse in both wire forms (per-item with noise and a torn item; the old single marker wrapped at 160 B), the storage bar follows [CLIPFS], an empty list says so"),
    ("clips_record_rename_delete", "Record saves under the typed name; a rename re-points the actions that name the clip, a clash or a board refusal changes nothing; a delete offers to remove its actions"),
    ("clip_record_refused", "(should) A Record the board refuses because it is replaying does not arm Stop & Save (the tool waits for a [CLIPUL:REC] marker no firmware prints)"),
    ("clip_download_verified", "A clip downloads to a file only when every event arrived: a lost line is re-requested by index; a truncated buffer, a mid-download change or a missing event save nothing"),
    ("clip_backup_bundle", "All + config warns with size and time first, then writes the config and every clip that downloaded completely, naming the one that did not"),
    ("clip_restore", "A restore asks per collision (skip, a re-checked new name, overwrite), writes at each index so a lost ACK's retry adds no duplicate, reads the list back and reports"),
    ("clip_restore_mode", "(should) A restored clip keeps the mode it was recorded in (D-NC33)"),
    ("clip_restore_bridged", "Over the WCB an event line too long for one ESP-NOW packet is refused and the board left out of edit mode; a small clip restores through [TERM:20] ACKs"),
    ("timeline_editor_save", "The timeline loads through the verified download and saves exactly its model; an incomplete clip opens read-only; closing mid-save cancels and leaves the clip as it was"),
    # Export / Import, two tabs on one WCB, the live panels.
    ("export_import_json", "Export writes the whole config; after a board reset, Import of that file and a Save put the board back exactly; a Wizard file, a foreign JSON and {} are refused; an old file empties the full-replace branches (pinned)"),
    ("csv_roundtrip", "(should) The legacy CSV export imported straight back leaves nothing for Save to send (today every button band narrows from +-12 to +-10)"),
    ("shared_hub_two_pages", "Two tabs on one WCB: the owner holds the port, the follower handshakes through it and reads the same lines, DTR is never asserted, and the follower takes the port over when the owner closes"),
    ("multi_tab_save", "(should) With two tabs on one WCB, one tab's save ACK does not confirm the other tab's save (D-NC35)"),
    ("live_monitor", "The SBUS panel follows the frames; 4 s of silence marks it stale and re-sends START_MONITOR once, which brings the frames back; after a disconnect nothing is re-sent"),
    ("wcb_status_panel", "The WCB chips follow the polled roster in both reply shapes; names come from WCB_META, asked at most 8 times while a board stays nameless; a learned peer is forgotten and stays gone"),
)
for _key, _title in _L1:
    def _l1(bench, _id=f"nctool.{_key}"):
        run_wizard_test(bench, _id, device=None)
    test(f"nctool.{_key}", f"{_title} (L1, no board: the emulator)")(_l1)


# L2: the real NaviCore through the pipe, inside nc_guard (NAVICORE.md §5.4 L2 table).
@test("nctool.board_connect_config", "L2: the config tool connects to the real NaviCore through the harness's own COM "
      "handle (no reset), applies its CONFIG, and a Save right after sends nothing; the tool's DTR/RTS are recorded, "
      "never applied", needs=["navicore"])
def board_connect_config(bench):
    """The pipe's proof (INF7). Read-only by design: the spec refuses to press Save when a diff exists, so a phantom
    diff fails it without writing the droid's config; nc_guard restores anyway if anything did land."""
    with nc_guard(bench):
        run_wizard_test(bench, "nctool.board_connect_config", device="navicore", pipe=True)


# ------------------------------------------------------------------ L2 helpers
SLBL_KEYS = ("S3", "S4", "S5", "maestro")     # RC_SLBL_KEYS (rc_config.h:575-702); the tool's SLBL_KEYS, index.html:3767
FRAG_MAX_ENV_BYTES = 187                      # sendJSON's single-packet limit over the bridge (index.html:5507, :5648-5652)
PARTITIONS_BIN = "NaviCore.ino.partitions.bin"   # arduino-cli's table beside the bench image (hil/ncflash.py build)
CLIPS_BYTES = 0xC00000                        # partitions.csv:21, the 12 MB clips LittleFS


def _quiet(nc):
    """RAM only: the dispatch trace off and no PWM_UPDATE stream, so the pipe carries only what the page asks for."""
    nc.set_debug_flags(0)
    nc.ack({"type": "STOP_MONITOR"})


def _free_slot(cfg):
    """(mode, button) of the first button slot GET_CONFIG `cfg` maps nothing to, for an editor the spec opens and closes
    unsaved (a new mapping's editor starts with one blank action row). Skip when every slot of 3 modes x 36 is mapped."""
    maps = cfg.get("mappings") or {}
    for mode in (1, 2, 3):
        for btn in range(1, 37):
            if str(mode * 100 + btn) not in maps:
                return mode, btn
    raise Skip("every NaviCore button slot has a mapping: no free slot for a scratch editor")


def _labels_problems(before, after, want):
    """What GET_CONFIG `after` changed from `before` other than serialLabels becoming exactly `want` (key names and
    redacted key paths only)."""
    got = after.get("serialLabels") or {}
    problems = []
    if got != want:
        wrong = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
        problems.append(f"serialLabels differs from what was typed at {wrong}")
    rest = redacted_diff({k: v for k, v in before.items() if k != "serialLabels"},
                         {k: v for k, v in after.items() if k != "serialLabels"})
    if rest:
        problems.append("GET_CONFIG changed besides serialLabels: " + "; ".join(rest))
    return problems


# ------------------------------------------------------------------ L2: NaviCore piped
@test("nctool.board_save_one_field", "L2: a port label typed in the Config window's Serial Ports tab and saved reaches "
      "the real NaviCore as one SET_CONFIG, ACKed; GET_CONFIG then differs from before by that label alone, a Refresh "
      "reads it back as the baseline and a second Save sends nothing (the config is restored after)", needs=["navicore"])
def board_save_one_field(bench):
    """nct.save.diff on the board: saveConfigToBoard ships only the branches _diffConfigBranches finds (index.html:
    16889-16957), and serialLabels is replaced whole when present (rc_config.h:1871-1878). The harness picks a label key
    NaviCore has not set and a HIL label, and proves the result over its own GET_CONFIG; nc_guard puts serialLabels back
    (an absent branch is cleared with {}, hil/nc_guard.py with_clears). A label rides NaviCore's WDP advert, so the WCBs
    learn it and lose it again with the restore."""
    with nc_guard(bench) as g:
        have = g.before.get("serialLabels") or {}
        key = next((k for k in SLBL_KEYS if not have.get(k)), None)
        if key is None:
            raise Skip("NaviCore has every port label set: no free key to type one into")
        label = marker("L")
        _quiet(g.nc)
        run_wizard_test(bench, "nctool.board_save_one_field", device="navicore", pipe=True,
                        args={"key": key, "label": label})
        problems = _labels_problems(g.before, json.loads(read_config(g.nc)), {**have, key: label})
    assert not problems, "; ".join(problems)


@test("nctool.board_test_action_wire", "L2: an action row's Test in the real tool fires TEST_ACTION on NaviCore with the "
      "row's unsaved values, and the wcb_unicast it carries (';S2' + a marker to WCB 1) comes out of W1 S2 exactly once; "
      "nothing is saved", needs=["navicore", "wcb1"])
def board_test_action_wire(bench):
    """nct.edit.test_action on the board (testActionRow, index.html:15521-15546): NaviCore dispatches the one action as a
    press would (the TEST_ACTION handler, NaviCore.ino:4017), so W1 runs ';S2<marker>' and writes '<marker>\\r' out of
    S2 (s41 _act). The editor is a free slot's, opened and closed unsaved; the config must be byte-identical afterwards
    without nc_guard's help."""
    l = link(bench, 1, "S2")
    l.listen()
    with nc_guard(bench) as g:
        mode, btn = _free_slot(g.before)
        text = marker("T")
        _quiet(g.nc)
        pm = l.mark()
        run_wizard_test(bench, "nctool.board_test_action_wire", device="navicore", pipe=True,
                        args={"wcb": 1, "port": "S2", "mode": mode, "btn": btn, "text": text})
        n = l.received(pm).count(text.encode() + b"\r")
        after = read_config(g.nc)
        changed = [] if after == g.before_text else config_diff(g.before_text, after)
    assert not changed, "a Test changed NaviCore's config: " + "; ".join(changed)
    assert n == 1, f"W1 S2 got the marker {n} times, not once"


@test("nctool.board_live_grid", "L2: the tool's live channel grid follows the real SBUS input: the bench controller's rx "
      "stick, moved through the bridge's /sbus route, shows its exact SBUS count on its channel's row, then the rest "
      "value again", needs=["navicore", "sbus"])
def board_live_grid(bench):
    """nct.live.pwm_update on the board: PWM_UPDATE carries NaviCore's decoded channels (sendPWMUpdate,
    NaviCore.ino:3131) and updateChannelsGrid shows each as fmtVal's SBUS count (index.html:11379-11432, :3418-3420). The
    rx stick's channel must be one NaviCore binds to nothing and rest at centre (s42 _stick); each count's axis argument
    is s42's axis_arg, checked here first with #L09 so a miss can be told apart from a controller mapping error. The
    stick is centred again whatever happens (the spec does, and so does this)."""
    ctl, nc, cfg, ncfg = _sbus_setup(bench)
    ch = _stick(nc, cfg, ncfg, "rx")
    rest = axis_value(cfg, "rx", 0.0)
    points = []
    try:
        for value in (rest + 300, rest - 300):
            arg = axis_arg(cfg, "rx", value)
            ctl.axes(0, 0, arg, 0)
            time.sleep(0.4)
            bad = _check_value(nc, ch, value, f"the rx stick at {value}")
            if bad:
                raise AssertionError("; ".join(bad))
            points.append({"arg": arg, "value": value})
    finally:
        ctl.axes(0, 0, 0, 0)
    with nc_guard(bench) as g:
        _quiet(g.nc)
        try:
            run_wizard_test(bench, "nctool.board_live_grid", device="navicore", pipe=True,
                            args={"ch": ch, "rest": rest, "points": points})
        finally:
            ctl.axes(0, 0, 0, 0)


@test("nctool.board_clips_list", "L2: the Clips panel lists the real NaviCore's clips by name, in ?REC,LS order, with "
      "the storage bar when the clips partition reports one; only ?REC,LS is sent and the clips are as before",
      needs=["navicore"])
def board_clips_list(bench):
    """nct.clips.list on the board, read-only (openClipsModal -> ?REC,LS, index.html:6164-6191; renderClips :6298-6349).
    The expected list is the harness's own ?REC,LS: [CLIPITEM] names in the order listClips prints them
    (navicore_record.h:960-982), [CLIPFS] only when the clips partition mounted (NaviCore.ino:3488). Greg's clips are
    only listed."""
    with nc_guard(bench) as g:
        lines, items = g.nc.clips()
        names = [c["name"] for c in items]
        _quiet(g.nc)
        run_wizard_test(bench, "nctool.board_clips_list", device="navicore", pipe=True,
                        args={"names": names, "storage": any(x.startswith("[CLIPFS]") for x in lines)})
        after = [c["name"] for c in g.nc.clips()[1]]
    assert after == names, f"the clip list changed: {len(names)} clips before, {len(after)} after"


@test("nctool.board_cmdlib_load", "L2: 'Load from NaviCore' pulls the real board's stored command library over USB: "
      "every board id in it is merged into the tool and its FNV-1a becomes the synced signature; nothing is written "
      "back and GET_CMDLIB_META is unchanged", needs=["navicore"])
def board_cmdlib_load(bench):
    """nct.cmdlib.custom_sync on the board, read-only: loadCmdlibFromBoard sends GET_CMDLIB (index.html:14500-14505) and
    the CMDLIB handler merges the boards and records msg.hash (:9159-9169); the merge counts every board with an id
    (_cmdlibMergeLibraryData :14248-14280). With no library stored NaviCore sends {"boards":[],"enums":{}} and its hash
    (NaviCore.ino:3884-3893), and the tool loads 0 boards. 'Save to NaviCore' is not pressed: a library written where none
    was stored cannot be removed (NAVICORE.md D-NC13)."""
    with nc_guard(bench) as g:
        meta = g.nc.cmdlib_meta()
        raw = g.nc.cmdlib()
        lib = json.loads(raw.decode("utf-8"))
        boards = lib.get("boards") if lib.get("boards") is not None else lib.get("components")
        ids = [b["id"] for b in (boards if isinstance(boards, list) else []) if isinstance(b, dict) and b.get("id")]
        mode, btn = _free_slot(g.before)
        _quiet(g.nc)
        run_wizard_test(bench, "nctool.board_cmdlib_load", device="navicore", pipe=True,
                        args={"ids": ids, "hash": fnv1a32(raw), "mode": mode, "btn": btn})
        after = g.nc.cmdlib_meta()
    assert after == meta, f"GET_CMDLIB_META changed: {meta} -> {after} (the load must not write the library)"


@test("nctool.emulator_contract", "L2: the in-Node emulator answers PING, GET_CONFIG, GET_CMDLIB_META, GET_WCB_STATUS, "
      "GET_MESH_STATS, ?REC,LS and ?OTALOCAL,STATUS in the real NaviCore's shapes (types, key sets, marker order), and "
      "its config model prints the board's own GET_CONFIG back byte for byte", needs=["navicore"])
def emulator_contract(bench):
    """NAVICORE.md §5.3: the L1 specs are only as good as the emulator (tests/wizard/lib/navicore/emulator.js), so a
    firmware change it has not followed fails here. No page: the spec drives the pipe and the emulator side by side
    (contract.spec.js, lib/navicore/contract.js) and reports differences by key path and type, never a value.
    Read-only; the debug flags and the monitor are quieted first so no stray line lands in a reply."""
    with nc_guard(bench) as g:
        _quiet(g.nc)
        run_wizard_test(bench, "nctool.emulator_contract", device="navicore", pipe=True)


@test("nctool.board_ota_usb_same_image", "OPT-IN (navicore_ota_full) L2: the tool's 'Update over USB (OTA)' streams the "
      "bench image NaviCore runs (GitHub's listing served by page.route) through the harness's own handle; NaviCore "
      "verifies it and restarts into the other slot with the same App SHA256, the tool reconnects by itself, and "
      "hil/ncflash puts the boot slot back (two restarts, ~3 min)", needs=["navicore"], links=[],
      opt_in="navicore_ota_full")
def board_ota_usb_same_image(bench):
    """nct.fw.ota_usb on the board: otaUpdateOverUsb's windowed ?OTALOCAL stream (index.html:17412-17607) against the
    real navicore_ota.h (BEGIN erases the inactive slot, DATA at the cursor, END verifies before the boot pointer moves
    and restarts 2 s later, :269-329), then reopenAfterFlash's reconnect across the USB re-enumeration (:17291-17359),
    which the pipe rides because the harness's SerialDevice reopens the port. Only the bench image, only while NaviCore
    runs it (s47 _on_bench_image); a FLASHED.md row for the tool's pass, and one for hil/ncflash's put-back pass, which
    ends NaviCore on the slot it started on. A pass that stops before END leaves the running app (the board verifies
    first); nc_guard proves the config, the command library and the clips unchanged."""
    nc = NaviCore(bench.dev("navicore"))
    info = _bench_image()
    _restartable(nc)
    what = "nctool.board_ota_usb_same_image"
    problems = []
    with nc_guard(bench, nc=nc) as g:
        start = _on_bench_image(g.nc, info)
        _idle(g.nc)
        _quiet(g.nc)
        failure = None
        try:
            run_wizard_test(bench, what, device="navicore", pipe=True, timeout=600,
                            args={"image": info["path"], "version": info["version"]})
        except AssertionError as e:
            failure = e
        after = ncflash._status_or_none(g.nc)
        moved = bool(after) and after["running"]["addr"] == start["next"]["addr"] and _runs(after, info)
        _record(info, "config tool Update over USB (OTA) through the pipe",
                (f"OK: {_slot(start['running'])} -> {_slot(after['running'])}" if moved else
                 f"{'FAILED' if failure else 'NOT PROVEN'}: running {_slot(after['running']) if after else '(no STATUS)'}"),
                what)
        if failure is not None:
            raise AssertionError(f"{what}: {_line1(failure)}; " + (
                f"NaviCore runs the bench image from {_slot(after['running'])}" if after and _runs(after, info)
                else ncflash.put_back()))
        if not moved:
            problems.append(f"after the tool's update NaviCore runs {_slot(after['running']) if after else '(no STATUS)'} "
                            f"App SHA256 {after.get('app_sha') if after else None}, not the bench image on "
                            f"{_slot(start['next'])}")
        if after and after["running"]["addr"] != start["running"]["addr"]:
            _flash_pass(g.nc, info, f"{what} put-back")
        end = _status(g.nc)
        if end["running"]["addr"] != start["running"]["addr"] or not _runs(end, info):
            problems.append(f"NaviCore ends on {_slot(end['running'])} {end.get('app_sha')}, not "
                            f"{_slot(start['running'])}; {ncflash.put_back()}")
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ L2: W1 piped
@test("nctool.board_via_wcb", "L2 through W1: the tool on W1's port connects 'Via a WCB' to the real NaviCore (bridged "
      "PONG, fragmented CONFIG applied with every mapping and nothing to save, W1 shown as the relay), and a port-label "
      "Save goes out as fragment envelopes of at most 187 bytes, is ACKed over the mesh, and changes GET_CONFIG by those "
      "labels alone", needs=["navicore", "wcb1"], links=[])
def board_via_wcb(bench):
    """nct.conn.via_wcb_hub and a bridged nct.save.diff on the board. The chooser's 'Via a WCB' (connectSharedPort,
    index.html:4676-4756), not 'Connect via USB': that one probes the port with bare JSON first, which W1 broadcasts
    (D-NC70, board_usb_probe_no_broadcast). The bridged CONFIG crosses W1's USB as fragment envelopes; W1's lines are not
    redacted by kind, so run_wizard_test's PipeLog keeps the envelopes' contents out of session.log. Every free label key
    gets a 24-character HIL label, so the Save is one branch over the 187-byte single-packet limit (sendJSON,
    :5635-5668) and travels in fragments that NaviCore reassembles and ACKs with the saveId over the mesh (rc_telemetry.h
    _applyReassembled). nc_guard restores the labels."""
    with nc_guard(bench) as g:
        have = g.before.get("serialLabels") or {}
        free = [k for k in SLBL_KEYS if not have.get(k)]
        if not free:
            raise Skip("NaviCore has every port label set: no free key to type one into")
        labels = {k: padded("L", 24) for k in free}
        want = {**have, **labels}
        inner = json.dumps({"sys": 1, "type": "SET_CONFIG", "data": {"serialLabels": want}, "saveId": 1},
                           separators=(",", ":"), ensure_ascii=False)
        if len(inner.encode("utf-8")) <= FRAG_MAX_ENV_BYTES:
            raise Skip(f"only {len(free)} port label key(s) free: the Save would fit one packet, not fragments")
        version = g.nc.ping()
        _quiet(g.nc)
        run_wizard_test(bench, "nctool.board_via_wcb", device="wcb1", pipe=True, timeout=400,
                        args={"labels": labels, "version": version, "relay": usb_wcb_number(bench),
                              "mappings": sorted((g.before.get("mappings") or {}).keys())})
        problems = _labels_problems(g.before, json.loads(read_config(g.nc)), want)
    assert not problems, "; ".join(problems)


@test("nctool.board_usb_probe_no_broadcast", "(should) L2 through W1: 'Connect via USB' on a tethered WCB puts nothing "
      "out of that WCB's serial ports; today the tool's direct probe types up to six bare JSON PINGs on W1's console, "
      "which W1 broadcasts out of its broadcast ports and onto the mesh before the tool falls back to Via WCB "
      "(NAVICORE.md D-NC70)", needs=["navicore", "wcb1"], links=[],
      drives=["W1S1", "W1S2", "W1S3", "W1S4", "W1S5"])
def board_usb_probe_no_broadcast(bench):
    """D-NC70. openPortAndStart PINGs a fresh port directly first (index.html:4568-4578); a WCB runs an unprefixed
    console line as a broadcast (handleSingleCommand, WCB.ino:6153-6164; processBroadcastCommand :8494-8563). Which W1
    ports put a broadcast out is W1's configuration, so this test finds out first: a bare marker line typed on W1's own
    console (the same broadcast) shows which probe-wired ports take one. Those are the ports the spec watches while the
    tool probes; with none the test skips."""
    w1 = usb_wcb(bench)
    wires = []
    for port in ("S1", "S2", "S3", "S4", "S5"):
        l = bench.links.usable(1, port)
        if l is not None:
            l.listen()
            wires.append((port, l))
    if not wires:
        raise Skip("no probe on any W1 port")
    tag = marker("BC")
    marks = {port: l.mark() for port, l in wires}
    w1.send(tag)
    time.sleep(1.5)
    took = [port for port, l in wires if tag.encode() in l.received(marks[port])]
    if not took:
        raise Skip("no probe-wired W1 port puts W1's own broadcasts out")
    bench.note(f"W1 ports that put a console broadcast out: {', '.join(took)}")
    with nc_guard(bench) as g:
        _quiet(g.nc)
        run_wizard_test(bench, "nctool.board_usb_probe_no_broadcast", device="wcb1", pipe=True, timeout=300,
                        args={"wires": [{"wcb": 1, "port": p} for p in took]})


# ------------------------------------------------------------------ L3: real Web Serial (attended)
def _hold_twin(bench):
    """Keep the SBUS controller's port open in the harness while Chrome has NaviCore's: both are the ESP32-S3's
    USB-Serial/JTAG (303A:1001), so a grant in .profiles/navicore could be the controller's, and the spec tries each
    granted port with those ids (lib/navicore/webserial.js connectGranted). Held here, the controller's open fails in
    Chrome instead of resetting it."""
    if bench.has("sbus"):
        bench.dev("sbus")


def _clips_partition_ok(nc):
    """[CLIPFS]'s total, when ?REC,LS reports one, is partitions.csv's 12 MB clips partition (LittleFS reports the
    partition's size less nothing we rely on: accepted from 11 MB up), or None when the partition did not mount."""
    lines, _ = nc.clips()
    fs_line = next((x for x in lines if x.startswith("[CLIPFS]")), None)
    if fs_line is None:
        return None
    total = int(json.loads(fs_line[len("[CLIPFS]"):]).get("total") or 0)
    return 11 * 2 ** 20 < total <= CLIPS_BYTES


@test("nctool.webserial_connect_reset", "ATTENDED OPT-IN (navicore_webserial) L3: the config tool opens NaviCore's own "
      "port over real Web Serial (Chrome's grant in .profiles/navicore; the first run asks someone to pick the port): "
      "the open restarts NaviCore, and the connect still succeeds on the direct link with its version, its CONFIG "
      "applied and nothing to save", needs=["navicore"], links=[], opt_in="navicore_webserial")
def webserial_connect_reset(bench):
    """nct.conn.direct over the real transport. Chrome asserts DTR/RTS inside open() and NaviCore's USB-Serial/JTAG
    resets the chip on that edge before openPortAndStart can deassert them (index.html:4482-4491, :4528-4540); the
    tool's 4 s settle must cover the boot so the direct probe answers. The restart is proven here by NaviCore's uptime
    (GET_MESH_STATS upMs below the time since, as s46 does), the spec notes whether the boot banner reached the tool.
    nc_guard puts back what a restart clears (the mode, via W1)."""
    nc = NaviCore(bench.dev("navicore"))
    _restartable(nc)
    _hold_twin(bench)
    with nc_guard(bench, nc=nc) as g:
        version = g.nc.ping()
        up0, t0 = g.nc.mesh_stats()["upMs"], time.monotonic()
        run_wizard_test(bench, "nctool.webserial_connect_reset", device="navicore", timeout=600,
                        args={"version": version})
        up1, elapsed_ms = g.nc.mesh_stats()["upMs"], (time.monotonic() - t0) * 1000
    assert isinstance(up0, int) and isinstance(up1, int), f"GET_MESH_STATS gave no uptime ({up0}, {up1})"
    assert up1 < up0 + elapsed_ms - 5000, (f"NaviCore did not restart while Chrome held its port: up {up0} ms, then "
                                           f"{up1} ms {elapsed_ms:.0f} ms later")


@test("nctool.webserial_flash_same_image", "ATTENDED OPT-IN (navicore_webserial) L3: the tool's Full Wipe & Flash over "
      "real Web Serial and esptool-js writes the custom bootloader, the partition table and the bench image NaviCore "
      "runs, erases NVS and otadata, and reconnects; NaviCore's config, command library and clips come through byte for "
      "byte (the texts say the config is erased, D-NC34) and it runs the bench image from app0", needs=["navicore", "wcb1"],
      links=[], opt_in="navicore_webserial")
def webserial_flash_same_image(bench):
    """nct.fw.flash_update and nct.fw.wipe_misleading on the board (runFirmwareFlash, index.html:17857-18007; flasher.js
    flashFirmware :267-423). What is written, and why each is safe: the app is the bench image, only while NaviCore runs
    it; the partition table is the bench build's, which the spec requires byte-identical to the one NaviCore publishes in
    firmware/ (and this test requires the board's layout to be partitions.csv's: STATUS's app0/app1 and a 12 MB clips
    FS); the bootloader is the NaviCore repo's custom one, the file every tool flash writes (flasher.js:193-202) - never
    the build's stock one. NVS holds NaviCore's learned peers only (WCB_Client 'wcb_peers'; its own 'rcfg' is a dead
    migration source, rc_config.h:2213-2220), which nc_guard has W1 re-advertise; otadata is erased so it boots app0.
    The config LittleFS (0x3D0000) and the clips (0x400000) are never written: their survival is the point (D-NC34).
    A FLASHED.md row for the flash; when NaviCore had run app1, hil/ncflash puts the boot slot back."""
    nc = NaviCore(bench.dev("navicore"))
    info = _bench_image()
    part = os.path.join(info["folder"], PARTITIONS_BIN)
    if not os.path.isfile(part):
        raise Skip(f"no {PARTITIONS_BIN} beside the bench image in results/builds/{ncflash.BENCH_IMAGE}")
    _restartable(nc)
    _hold_twin(bench)
    what = "nctool.webserial_flash_same_image"
    problems = []
    with nc_guard(bench, nc=nc) as g:
        start = _on_bench_image(g.nc, info)
        _idle(g.nc)
        slots = {s["label"]: (s["addr"], s["size"]) for s in (start["running"], start["next"]) if s}
        if slots != {"app0": (0x10000, 0x1E0000), "app1": (0x1F0000, 0x1E0000)} or not _clips_partition_ok(g.nc):
            raise Skip("NaviCore's partition layout is not partitions.csv's (app0/app1 and a 12 MB clips FS): the flash "
                       "would write a table the board does not hold")
        meta, clips = g.nc.cmdlib_meta(), g.nc.rec_ls()
        failure = None
        try:
            run_wizard_test(bench, what, device="navicore", timeout=1200,
                            args={"image": info["path"], "part": part, "version": info["version"]})
        except AssertionError as e:
            failure = e
        after = ncflash._status_or_none(g.nc)
        good = bool(after) and after["running"]["label"] == "app0" and _runs(after, info)
        _record(info, "config tool Full Wipe & Flash (esptool-js, L3)",
                (f"OK: {_slot(start['running'])} -> {_slot(after['running'])}" if good else
                 f"{'FAILED' if failure else 'NOT PROVEN'}: running {_slot(after['running']) if after else '(no STATUS)'}"),
                what)
        if failure is not None:
            raise AssertionError(f"{what}: {_line1(failure)}; " + (
                f"NaviCore runs the bench image from {_slot(after['running'])}" if after and _runs(after, info)
                else ncflash.put_back()))
        text = read_config(g.nc)
        if text != g.before_text:
            problems.append("NaviCore's config changed across the Full Wipe: " + "; ".join(config_diff(g.before_text, text)))
        if g.nc.cmdlib_meta() != meta:
            problems.append("its command library changed across the Full Wipe")
        if g.nc.rec_ls() != clips:
            problems.append("its clips changed across the Full Wipe")
        if not good:
            problems.append(f"after the flash NaviCore runs {_slot(after['running']) if after else '(no STATUS)'} "
                            f"App SHA256 {after.get('app_sha') if after else None}, not the bench image from app0")
        if after and after["running"]["addr"] != start["running"]["addr"]:
            _flash_pass(g.nc, info, f"{what} put-back")
    assert not problems, "; ".join(problems)
