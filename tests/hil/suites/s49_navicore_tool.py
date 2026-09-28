"""The NaviCore config tool (NaviCore/config_tool/index.html), ids nctool.* (docs/hil_plan/NAVICORE.md §5, NC-WP3 and
NC-WP13). Each test runs the Playwright spec of the same id in tests/wizard/specs/navicore, or the node tests of
tests/wizard/unit/navicore, the way s30_wizard.py runs the Wizard's.

  L0  nctool.static, nctool.unit      node --test: the page's syntax, the firmware/tool constant pairs, the page's own
                                      pure functions lifted into node, and the rig's model of the firmware
  L1  nctool.<spec>                   the real page with a fake navigator.serial backed by an in-Node NaviCore
                                      emulator; no board, no port, nothing on the bench changes (device=None)
  L2  nctool.board_*                  the same fake port piped to the REAL NaviCore through the harness's own COM
                                      handle (run_wizard_test(..., pipe=True)), inside nc_guard

(should) specs assert what the tool ought to do and fail until it does (docs/HIL_TESTING.md §6); in Playwright they are
test.fail(), so a standalone or CI run stays green and turns red the day the fix lands. They need the NaviCore repo beside
this one (tests/wizard/lib/navicore/paths.js); without it every nctool test skips.
"""
from hil.nc_guard import nc_guard
from hil.runner import test
from hil.wizard import run_unit_tests, run_wizard_test


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
    ("doorway_pong_misdetect", "(should) A PONG relayed through a WCB is not taken for a direct link: Via WCB, and USB OTA disabled (D-NC30)"),
    ("rx_framing_markers", "Lines cut anywhere (a UTF-8 character included) reassemble; [MAE:], [OTA:], [TERM:] and plain text go where they belong; debug lines follow their chip; the pane keeps 3000 lines"),
    ("rx_fragments", "A fragmented CONFIG applies once whatever the order, duplicates and noise; broken envelopes are ignored; an idle session expires and never completes"),
    ("config_error_no_baseline", "A GET_CONFIG answered with an ERROR leaves the tool unloaded, and Save warns before pushing its defaults"),
    ("apply_all_keys", "Every key GET_CONFIG carries lands in the tool unchanged but for four documented normalizations, and the General and WCB fields show it"),
    ("no_phantom_diff", "Opening every Config tab and editor, then Save, sends nothing; the last tab is remembered and a stale name falls back to channels"),
    ("save_diff_payload", "An edit typed into General is the only thing Save sends; the hold clamp matches the firmware; a second Save sends nothing"),
    ("save_ack_correlation", "A NACK and a 12 s silence leave the baseline alone, an overlapping Save is refused, a stale saveId is ignored, an ACK with no saveId still counts"),
    ("reset_defaults_flow", "Restore Defaults asks, sends RESET_DEFAULTS then GET_CONFIG, reports success only once the CONFIG is in, warns after 12 s without one, and refuses over the bridge"),
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
