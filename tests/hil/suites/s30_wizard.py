"""The Wizard (browser config tool) against a real board over Web Serial. Each test hands W1's USB port to Chrome,
runs the matching Playwright test from tests/wizard/specs (same id), and takes the port back — the harness decides
what the board should look like and checks what it looks like afterwards; the browser test only drives the page.
See docs/HIL_TESTING.md § Wizard tests."""
from hil.runner import test
from hil.wcb import PULL_MAX, WCB, group_tokens
from hil.wizard import run_unit_tests, run_wizard_test
from suites.common import config_guard, link, marker, snapshot, token, usb_wcb
from suites.s03_wcb import SEQ_ROW, _clear, _factory_reply, _grow, _grow_over

# For the WCB-WP20/21/40/41 block at the end of this file.
import re
import time

from hil.runner import Skip
from suites.common import Console, nonce, prime
from suites.s12_serial_input import _da_forget_on, _da_scrub_on, _da_types_on


@test("wizard.parser", "Wizard/parser.js round-trips a real ?backup and its diff push sends only what changed")
def parser(bench):
    """No browser, no board: node --test over tests/wizard/unit. Also runs in CI (.github/workflows/wizard-tests.yml)."""
    run_unit_tests(bench)


@test("wizard.smoke", "The Wizard loads with no uncaught page errors")
def smoke(bench):
    run_wizard_test(bench, "wizard.smoke", device=None)


@test("wizard.kyber_release_order", "(should) A push that moves or leaves a local Kyber sends ?KYBER,CLEAR / ?MAESTRO,REMOTE ahead of the BAUD/BCAST lines and re-sends the released port's rows; a remote board's targets-only diff sends no release (no board: generator logic in the page)")
def kyber_release_order(bench):
    """Tracker #73 D5/D9: every way out of Kyber LOCAL now puts the old port back to 9600 with broadcasts on
    (kyberReleasePort, WCB_Storage.cpp), so a release sent after the push's own ?BAUD/?BCAST lines would undo them."""
    run_wizard_test(bench, "wizard.kyber_release_order", device=None)


@test("wizard.kyber_local_frees_other_port", "(should) A local Kyber claims only its own port (bare = S2), Maestro REMOTE keeps S1, and a push from REMOTE to local sends ?KYBER,CLEAR before an HCR on S1 (no board: claim and generator logic in the page)")
def kyber_local_frees_other_port(bench):
    """Tracker #73 D4: the firmware now reserves only the Kyber's own port (kyberModeReservesPort, WCB_Storage.cpp),
    so the Wizard offers the other hardware port to devices and PWM - and must release REMOTE's S1 reservation
    before the device lines of a REMOTE -> local push, or the board refuses the device."""
    run_wizard_test(bench, "wizard.kyber_local_frees_other_port", device=None)


@test("wizard.kyber_auto_targets", "autoComputeKyberTargets fills a local-Kyber board's targets from every connected board's Maestros (its own included), keeps every remembered target of an unconnected board, drops those of a connected one, and leaves a remote board alone (no board: page logic)")
def kyber_auto_targets(bench):
    """A remembered target is kept unless its board reported a live Maestro list. Keyed on the id alone until
    2026-09-24, which dropped a remembered M1 on W3 once any connected board had an M1 (docs/HIL_TEST_AUDIT.md F4)."""
    run_wizard_test(bench, "wizard.kyber_auto_targets", device=None)


# The remote pull against a fake relay in the page (tests/wizard/specs/remote_pull_fake.spec.js): no board, so CI runs
# them too. The real pulls through W1 are wizard.remote_pull and wizard.remote_pull_parts below (F13).
_FAKE_PULL = (
    ("api", "the page exposes the one parser API, crc32 matches zlib, and remoteBoardPull is on window"),
    ("legacy", "a single [MGMT:CONFIG,n] reply is stored exactly as before, and the pane shows only its length"),
    ("legacy_crc", "a legacy [MGMT:CONFIG,n] reply that fails its checksum, or has none, is retried and never stored; the slot keeps its config and baseline (W-7)"),
    ("parts", "three parts out of order, with a duplicate, noise and another board's lines, join into one verified config"),
    ("timer", "every NEW part restarts the 6 s attempt timer, a duplicate does not, and the retry is a new job"),
    ("fifo", "one attempt on the air per relay: a second board waits until the first settles"),
    ("errors", "CFGERR NOMEM and NOPARTS retry, TOOBIG stops at once, not-UTF-8 retries once and stops on a second job's, empty replies fail after 3 attempts, a CRC failure retries"),
    ("deadline", "parts that trickle in forever end the pull at PULL_DEADLINE_MS from the call, once, including a pull queued behind dead targets"),
    ("display", "a throwing listener costs no other listener its line, pull lines are summarised, a relayed or direct stats capture skips them"),
    ("reader", "parts read in small chunks, cut inside multi-byte characters, reassemble through both readers"),
    ("edges", "a relay that is not connected, a send that throws, and a superseded pull each settle once"),
)
for _key, _title in _FAKE_PULL:
    def _fake_pull(bench, _id=f"wizard.remote_pull_fake_{_key}"):
        run_wizard_test(bench, _id, device=None)
    test(f"wizard.remote_pull_fake_{_key}", f"{_title} (no board: a fake relay in the page, F13)")(_fake_pull)


# The push generator against pulled fake boards (tests/wizard/specs/push_fake.spec.js): no board, so CI runs them too.
# Each pins a Wizard defect from the coverage re-scan (docs/hil_plan/WCB.md section 3, W-1 to W-12).
_FAKE_PUSH = (
    ("noop", "every card of a pulled board, pushed with no edit, sends nothing (W-4, W-1, W-2)"),
    ("one_edit", "one edit on a pulled card sends that edit, an edit undone sends nothing, and a new Maestro or WLED still gets its defaults (W-4)"),
    ("etm_delay", "the General ETM delay keeps 0: shown, kept when another ETM field changes, and pushed when typed (W-1)"),
    ("delimiter", "the characters change in an order the board takes, a ',' board changes its delimiter first, and the General inputs refuse what the firmware refuses (W-2)"),
    ("bidir", "a mapping row touched but not changed re-sends no mapping, so a PWM input does not reboot the board (W-5)"),
    ("reboot_matchers", "the reboot and network-group checks read each command, not a substring of a label or a sequence (W-8)"),
    ("relay_cap", "a relay push over 16 chunks is refused before the network-group confirm, the bootstrap or any send (W-9, F14)"),
    ("relay_chars", "a relay push with a character change the board would refuse on the way sends nothing, and a ',' target takes ?D^ first (W-2)"),
    ("relay_push", "a relay push still asks before a network-group change, bootstraps a new function id first, and frames the chain in 179-char chunks"),
    ("seq_roundtrip", "an untouched sequence row gives back its stored value exactly; an edited row is sent as before (W-10)"),
)
for _key, _title in _FAKE_PUSH:
    def _fake_push(bench, _id=f"wizard.push_fake_{_key}"):
        run_wizard_test(bench, _id, device=None)
    test(f"wizard.push_fake_{_key}", f"{_title} (no board: pulled fake boards in the page)")(_fake_push)


@test("wizard.pull","The Wizard connects to W1 and shows its saved bauds and labels", needs=["wcb1"])
def pull(bench):
    with config_guard(bench, 1) as before:
        run_wizard_test(bench, "wizard.pull", args={"tokens": before[1]})


@test("wizard.push_label", "A label typed into the Wizard and pushed lands in W1's saved config", needs=["wcb1"])
def push_label(bench):
    with config_guard(bench, 1) as before:
        t = marker()
        try:
            run_wizard_test(bench, "wizard.push_label", args={"port": 5, "label": t})
            assert f"?LABEL,S5,{t}" in snapshot(bench, 1), "the pushed label is not in W1's ?backup"
        finally:
            orig = token(before[1], "?LABEL,S5,")
            usb_wcb(bench).run(orig if orig else "?LABEL,CLEAR,S5")


@test("wizard.terminal_wire", "A ;S1 typed into the Wizard terminal comes out of W1 S1", needs=["wcb1", "probe1"])
def terminal_wire(bench):
    link(bench, 1, "S1").listen()      # bound before Chrome takes W1, so the browser test only marks and expects
    run_wizard_test(bench, "wizard.terminal_wire", args={"wcb": 1, "port": "S1"})


def _remote_pull(bench, test_id, over):
    """Hand W1 to Chrome and let the real Wizard pull W2 through it (remote_pull.spec.js), with W2 grown on its own USB
    first: the bridge has no route to run a command on another board. The spec gets W2's firmware version, the
    throwaway keys with their value lengths, and the reply length - never a token. The chain carries the mesh password
    and the WiFi passphrase, and whatever the spec is given or fails with lands in its Playwright report and in
    session.log. W1 is guarded too: connecting runs the Wizard's own pull of it."""
    w2 = WCB(bench.dev("wcb2"))
    keys = []
    with config_guard(bench, 1, 2):
        try:
            ver = w2.version()
            if over:
                _grow_over(w2, ver, keys)
            else:                                   # one short sequence, so there is a value to see arrive whole
                n = _grow(w2, ver, keys, len(_factory_reply(w2, ver)) + SEQ_ROW + 100)
                assert n <= PULL_MAX, f"W2's reply is {n} characters; this test needs a one-line config (<= {PULL_MAX})"
            chain = _factory_reply(w2, ver)
            seqs = {k: len(t) - len(f"?SEQ,SAVE,{k},") for k in keys
                    for t in group_tokens(chain[chain.index("]") + 1:].split("^")) if t.startswith(f"?SEQ,SAVE,{k},")}
            assert sorted(seqs) == sorted(keys), f"W2's ?backup lacks {sorted(set(keys) - set(seqs))}"
            bench.note(f"{test_id}: W2's reply is {len(chain)} characters, throwaway sequences {seqs}")
            run_wizard_test(bench, test_id, args={"relay": 1, "target": 2, "ver": ver, "seqs": seqs,
                                                  "length": len(chain), "minParts": 2 if over else 0})
        finally:
            w2.run("?RTERM,STOP")                   # a pull the Wizard completes starts W2's remote terminal (RAM only)
            _clear(w2, keys)


@test("wizard.remote_pull", "The Wizard, connected to W1 only, pulls W2 through it with ?MGMT,PULL,2,P and gets the one [MGMT:CONFIG,2] line: onComplete fires once with true, and W2's card has its firmware version and its sequence whole (F13)", needs=["wcb1", "wcb2"])
def remote_pull(bench):
    _remote_pull(bench, "wizard.remote_pull", over=False)


@test("wizard.remote_pull_parts", "The Wizard, connected to W1 only, pulls W2's config of over 2912 characters through it as 2 [MGMT:CFGPART,2] parts, joins and checks them: onComplete fires once with true, every sequence arrives whole, and no raw part text reaches the terminal (F13)", needs=["wcb1", "wcb2"])
def remote_pull_parts(bench):
    _remote_pull(bench, "wizard.remote_pull_parts", over=True)


# ======================================================================================================================
# WCB-WP20, WP40 and WP41 (docs/hil_plan/WCB.md §2): the rest of the push side, the app.js logic and panels, the
# editors and the flasher, against fake boards in the page - no board, so CI runs them too (wizard-tests.yml runs every
# spec). Specs: push_more_fake, app_fake, editors_fake, flasher_fake. The WP40 unit tests (unit/model.test.js) run
# under wizard.parser with the rest of tests/wizard/unit.
# (should) specs assert what the Wizard ought to do and fail until it does: test.fail() in Playwright keeps CI green,
# and the harness reports them FAIL (docs/HIL_TESTING.md §6). Each names its W-row in docs/hil_plan/WCB.md §3.
_FAKE_MORE = (
    ("push_fake_card_edits", "One edit on each card push_fake leaves out sends only that edit: DFPlayer volume, the Kyber Marcuino port, a sequence row, the alias; a mapping edit re-sends every mapping (left as found)"),
    ("push_fake_kyber_own_maestro", "(should) A local-Kyber board with a Maestro of its own and one on another board, pulled and pushed with no edit, sends nothing (W-20)"),
    ("push_fake_relay_reboot", "A relay push that needs a reboot ends its session with ^?reboot (not with skipReboot), a MAC or channel change asks first and Confirm sends it, and the baseline moves to the pushed config"),
    ("push_fake_all_staged", "Push All: remote boards first with the reboot inside their session, then the USB boards, then the relay without a reboot, the relay rebooted last; a MgmtRelay card never pushed, the General dirty flag cleared"),
    ("push_fake_all_shared_relay", "(should) Push All with the relay on the shared port reboots it without reporting it lost, and its card stays connected (W-15)"),
    ("push_fake_reboot_path", "A USB push that needs a reboot sends ?reboot 1.5 s after the last ACK, closes and reopens the port, and pulls 3 s after the reconnect"),
    ("push_fake_shared_reboot_repull", "(should) A push that reboots a board on the shared port pulls it again afterwards, as a USB push does (W-14)"),
    ("pull_fake_usb_guards", "The USB pull: a cut backup changes nothing, the fixed pull command goes first, ?RELAY,1 becomes a relay card, a board reporting another number moves slot, a duplicate number warns, the first pull seeds General"),
    ("push_fake_general_fields", "The General fields reach every board's push: a differing password opens the keep/use modal; Keep sends the first board's (with the network-group confirm through a relay), Use incoming rewrites General"),
    ("push_fake_planned_verbs", "The bench specs' pre-flight (tests/wizard/lib/wizard.js plannedVerbs) names the verbs a push then sends, directly and through a relay, sends nothing itself, and would stop a push carrying another board's WCB quantity"),
    ("push_fake_general_wcbq", "(should) A second board whose WCB quantity differs is named in the keep/use modal, like every other General field every push writes (W-16)"),
    ("app_fake_controller", "The Controller selector stashes each controller's board roles and restores them on the way back, writes the controller only into WCB slots, and follows the NaviCore id"),
    ("app_fake_etm_listener", "A relay's ONLINE edge pulls the board and re-arms its terminal, repeated OFFLINE edges start one verify pull, and during an OTA the edges are held for later"),
    ("app_fake_wdp_panel", "parseWdpDump reads every firmware dump line (PEER, WDPIF, WDPDA with SEEN/AGE, WDPX, WDPPWM, WDPCFG EN); the panel marks a quiet device not heard; Forget, Clear, AutoJoin and Poll send their commands"),
    ("app_fake_mesh_tick", "The 12 s discovery tick surfaces unknown WCBs with their alias and no pull, gives clients a card, drops a quiet temporary client and marks others Offline, and skips during a push, flash, OTA or stats capture"),
    ("app_fake_partial_ack", "ACK pacing retries once; no answer after the retry fails the push (the rest still sent) and pulls to check; an unconnected board is refused; a remote PWM destination board is pulled after the push"),
    ("app_fake_serial_claims", "A device's port shows its broadcast boxes cleared but never pushes them; an unclaimed port's box change is pushed"),
    ("app_fake_terminal", "The debug toggles send ?DEBUG,<mode>,ON/OFF per board (through the relay too) and reset on disconnect; the terminal filter, ?TERMDEBUG, a hand-typed ?WDP,DUMP, timestamps and pane visibility"),
    ("app_fake_setup_wizard", "The guided setup validates its steps, applies Kyber local with every Maestro as a target and the other Maestro board as remote, and its export parses back to the same boards"),
    ("app_fake_system_file", "The export writes every board the Wizard knows (the floor, a board above it with its labels, sequence and variable, a client slot with its token) under the General WCB quantity, and refuses two boards on one number"),
    ("app_fake_system_file_reload", "(should) A saved system file loads back as it was saved: its WCB quantity, no board it did not hold, a board above the floor whole, so saving it again writes the same file (W-17)"),
    ("app_fake_identity", "The identity fields: LED pin preset or custom 0-48, a 3.1 board loads clean, a new WCB number pushes ?WCB with a reboot, the alias cut at 24 and cleaned, a client slot never pushed"),
    ("app_fake_wcb_number_above_floor", "(should) A board above the WCB quantity can be renumbered to any number its dropdown offers (W-18)"),
    ("app_fake_fw_check", "The latest-release check: the version from the bin name, the update badge behind it, up to date on it, dev ahead of it, a malformed version harmless, the check throttled, a stale branch override warned"),
    ("app_fake_relay_card", "A MgmtRelay card lists the WCBs its mesh hears, Manage all arms their terminals and pulls one at a time, a second Manage all only re-arms, Push All and the export leave it out, a disconnect un-manages"),
    ("app_fake_ota_baud_fallback", "An OTA over USB proves its raised baud: a board deaf at 921600 is left to its session timeout with nothing sent, begun again and streamed at 460800, the rate remembered so the next OTA starts there; a rate it answers at is used at once"),
    ("app_fake_hub_flash_refused", "A flash or an erase on the shared port is refused while this tab only follows the hub or another tab waits on its lock; nothing is flashed and the outcome says the push did not run"),
    ("app_fake_pull_leaves_nothing_pending", "(should) Pulling a board leaves nothing pending in General: no 'push to all boards' toast, Push All not flagged (W-19)"),
    ("app_fake_pending_funcchar", "(should) A function identifier typed into General but not yet pushed is not used for the board's immediate commands (W-13)"),
    ("editors_fake_mappings", "The mapping editor sends at once: Save with bidir puts the reverse on the destination board through the relay, Remove sends CLEAR, a removal while disconnected is only local (left as found), a moved PWM output is cleared"),
    ("editors_fake_bidir_remove", "(should) Removing a bidirectional serial mapping also clears its mirror on the destination board (W-21)"),
    ("editors_fake_seq_var", "The sequence and variable editors: save, rename, test, remove; a remote save in one packet or as a multi-chunk session; variables set, rename, clear, a bad name refused, ;V for a temporary; ?VAR,LIST parsed, relay too"),
    ("editors_fake_live_controls", "The WLED controls send ;L<id>,<verb> directly or through the relay; the HCR status modal renders the firmware's line, polls every 3 s only while open, and says when HCR is not configured"),
    ("flasher_fake_helpers", "The flasher's decisions: the detected chip wins, S2 and an unknown chip with no HW refused, the S3 bootloader by flash size (none when unknown), a changed partition table makes an update full, the branch from /dev/ or a valid override"),
)
for _key, _title in _FAKE_MORE:
    def _fake_more(bench, _id=f"wizard.{_key}"):
        run_wizard_test(bench, _id, device=None)
    test(f"wizard.{_key}", f"{_title} (no board: fake boards in the page)")(_fake_more)


# The untested high-risk Wizard paths of the 2026-10-12 coverage re-scan (docs/hil_plan/RESCAN_2026-10-12.md; rows in
# docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each title): the push guards and the flash and erase
# paths (specs/push_flash_fake.spec.js), the read loop's reconnect, the shared-port decision and the setup wizard's
# Connect & Push step (specs/connect_wizard_fake.spec.js). No board and no real port (a fake navigator.serial), so CI
# runs them too. wizard.wiz_fake_connect_step is its row's fakeable half: a reset pressed on a real board stays attended.
_FAKE_RESCAN = (
    ("push_fake_var_guard", "A variable row with an invalid name stops the push, the relay push and the export before anything is sent, so the diff cannot clear the variable it renamed; a temporary row is never in the config (var.push_export_guard)"),
    ("push_fake_mac_validate", "A General MAC octet that is not hex turns red when left and a single digit is padded; a push, direct or through a relay, checks the octets itself and sends nothing for a bad one; Push All checks them once before any board starts, one message, nothing pushed (general.mac_validate)"),
    ("flash_fake_entry_snapshot", "Flash, Update and Factory close the port first, show Flashing... while flashFirmware runs with appOnly for Update and eraseNvs for Factory, and put the card back after a flash that worked or failed (flash.entry_snapshot)"),
    ("flash_fake_post_success", "After a flash the port is reopened and the firmware waited for: Flash pushes the whole pre-flash config with its General fields, Update pulls (pushes in the setup wizard), Factory unchecked pulls, a borrowed shared port is given back, a board that never returns is said to (flash.post_success)"),
    ("flash_fake_erase_path", "The erase sends ?ERASE,NVS, keeps the port open 4 s, reopens it, waits for the firmware and pushes the whole pre-erase config with its General fields; Erase NVS Only pulls instead (flash.erase_path)"),
    ("connect_fake_port_loss_reconnect", "A port that drops on its own is reopened (10 x 1.5 s), the relay's boards re-armed (not one now on USB) and the board pulled 3 s later outside the setup wizard; a MgmtRelay that never returns loses its card (connect.port_loss_reconnect)"),
    ("connect_fake_establish_autoshare", "The first board connected is the shared port unless another tab leads or the bulk detect opted out, two at once share one, a dead share falls back to direct; a busy port joins another tab's share only when it is surely that port, and backs out on a VID/PID mismatch (connect.establish_autoshare)"),
    ("wiz_fake_watch_push_verify", "The setup wizard's watcher pushes the wizard's config whole, waits for the reconnect, verifies with up to three pulls and reports Done (next WCB, auto-close), Not pushed, Push incomplete, Unverified or No reconnect (wiz.watch_push_verify)"),
    ("wiz_fake_connect_step", "The setup wizard's Connect & Push row: Detect ignores the drain's resets and takes the port reset after it, a connect clears a stale baseline so the push waits for the pull; Select by COM#, Authorize, Cancel, Reconnect, the 30 s timeout (wiz.connect_step, fakeable half)"),
)
for _key, _title in _FAKE_RESCAN:
    def _fake_rescan(bench, _id=f"wizard.{_key}"):
        run_wizard_test(bench, _id, device=None)
    test(f"wizard.{_key}", f"{_title} (no board: fake boards and ports in the page)")(_fake_rescan)


# The medium-risk Wizard paths of the same re-scan (rows with risk M, named at the end of each title): the General
# section (specs/general_rescan_fake.spec.js), the board grid and the cards' identity, serial and WiFi fields
# (specs/cards_rescan_fake.spec.js), the Kyber and NaviCore cards (specs/kyber_navicore_rescan_fake.spec.js), the audio
# devices (specs/audio_rescan_fake.spec.js), the row editors (specs/editors_rescan_fake.spec.js), the Connect button,
# BoardConnection's reconnect and I/O primitives (specs/connect_rescan_fake.spec.js), the shared-port hub and the
# wrong-slot pull (specs/hub_rescan_fake.spec.js), the flasher's choices and write (specs/flasher_rescan_fake.spec.js),
# the flash and OTA paths around it (specs/flash_ota_rescan_fake.spec.js), the system file and the setup wizard
# (specs/file_wiz_rescan_fake.spec.js). No board and no real port, so CI runs them too. The parser row of the batch
# (parser.backup_tokens) is a node test, tests/wizard/unit/legacy_vars.test.js, run by wizard.parser.
_FAKE_RESCAN_M = (
    ("general_fake_wcbq_change", "WCB Quantity: More... lists 1-20, a pick sets the floor (the grid grows and shrinks, a connected board above it stays), widens every WCB# dropdown and marks General pending (general.wcbq_change)"),
    ("general_fake_password_mac_channel", "The password, MAC octets and mesh channel edited in General reach every board's config and the General baseline, turn Push All amber, and every board's push carries them (general.password_mac_channel)"),
    ("general_fake_etm_toggle_checksum", "ETM Enabled off pushes ?ETM,OFF alone and on ?ETM,ON alone, a board pulled with ETM off keeps its details; Packet Checksum is never pushed OFF and a board with it off is put back ON (general.etm_toggle_checksum)"),
    ("general_fake_etm_fields", "The ETM numbers: the message count clamps to 10-200, an emptied field falls back to the firmware default, and every board's config and push follow (general.etm_fields)"),
    ("general_fake_mismatch_modal", "The keep/use modal: none for a factory-default board or with the setup wizard open; Use takes the NaviCore controller and id and marks the other boards unsaved; after a file load it names 'loaded file' (general.mismatch_modal)"),
    ("grid_fake_reconcile", "The board grid is the floor plus mesh-reported boards plus live connections, in order; a shrinking floor removes idle sections with their configs but never a connected or mid-flash board (grid.reconcile)"),
    ("identity_fake_hw_version", "A board pulled with HW 0 pushes no ?HW line, nor does picking Select; the LED pin group shows for 3.1/3.2 only, and only a user's pick marks the board unsaved (identity.hw_version)"),
    ("serial_fake_baud_options", "The serial table offers S3-S5 up to 57600 plus the configured baud marked (!): a board pulled with S4 at 115200 shows it, and pushed untouched sends nothing (serial.table_baud_options)"),
    ("wifi_fake_ui", "The WiFi radios show the AP or Join fields with a note naming the mesh channel, Off hides them; mode and credentials go into the config, the board is marked unsaved, and the push sends one ?WIFI line (wifi.ui)"),
    ("kyber_fake_mode_radio", "The Kyber radio: Local claims S2 as Kyber Maestro, shows its fields, marks the board unsaved and pushes the port rows then ?KYBER,LOCAL,S2; None clears the automatic label; Remote pushes ?MAESTRO,REMOTE (kyber.mode_radio)"),
    ("kyber_fake_port_dropdown", "The Kyber port picker offers S1/S2 only, hides a port another feature holds, keeps the current one, shows a legacy S3-S5 port disabled, and says when both are taken - a push of Kyber Local then is refused with a message, nothing sent (kyber.port_dropdown)"),
    ("kyber_fake_baud", "A Kyber baud change is written into the Kyber port's serial baud, so the push carries ?BAUD,S<port> and no Kyber line (kyber.baud)"),
    ("navicore_fake_remote_radio", "Under NaviCore only its card shows; the Remote radio sets the board's Kyber mode to remote, the status names the id, the push sends ?MAESTRO,REMOTE; None takes it back (navicore.remote_radio)"),
    ("navicore_fake_reconcile_maestros", "A board that gains a Maestro under NaviCore goes remote, and one that loses its last Maestro leaves remote (navicore.reconcile_maestros)"),
    ("audio_fake_toggle_and_mode", "An audio device's enable box shows its block and starts Local; a Local port pushes ?HCR,PORT and ?HCR,POLL, unchecked ?HCR,CLEAR, Remote ?HCR,REMOTE,W<n>, leaving Remote ?HCR,REMOTE,OFF first (audio.toggle_and_mode)"),
    ("audio_fake_port_dropdowns", "Device port pickers offer only free ports and their own, every picker follows a device taking or freeing a port, and an HCR on S3-S5 is clamped to 57600 in its picker, config and serial row, as its tooltips say (audio.port_dropdowns)"),
    ("audio_fake_clamps", "MP3 volume 0-64, DFPlayer volume 0-30, HCR poll 0 or 3-3600 (shown in the field), on edit and again at push time (audio.clamps)"),
    ("audio_fake_route_host", "The Remote host picker offers what a mapping destination offers - the quantity's floor and mesh-reported boards above it - without this board, never selects this board, keeps a pulled host (audio.route_host_dropdown)"),
    ("wled_fake_rows", "A new WLED row takes the lowest id no local or remote WLED uses, its port picker offers only free ports, Remove renumbers; S3-S5 cap the bauds at 57600, for a row built there and once a port is picked (wled.rows)"),
    ("maestro_fake_rows", "A new Maestro row is id 1 at 115200; its port picker hides ports HCR, WLED, a local Kyber or another row hold but keeps S1 under REMOTE; S3-S5 cap at 57600; Remove renumbers and the last one out leaves remote (maestro.rows)"),
    ("maestro_fake_routing_table", "A Maestro edit rebuilds every board's routing table and a local Kyber's targets, marks the changed boards unsaved, toasts the re-push count, and the other boards' pushes send the whole ?MAESTRO table (maestro.routing_table)"),
    ("mapping_fake_rows", "A mapping card's destination list covers the quantity, mesh-reported boards and a pulled W9; the type switch hides Raw/Bidir for PWM and drops S0; the source list offers free, PWM and serial-map ports (mapping.rows)"),
    ("seq_fake_validate_if", "IF inside a ;t or ;w payload is refused on Test, Save/Update and Push (direct or relayed) with a toast naming IF, nothing sent; IF ahead of the token is not, nor a row the board already holds (seq.validate_if)"),
    ("connect_fake_modal_and_button", "Connect opens the modal (Auto-Detect, manual, Authorize, Remote via each connected board, Share); Remote arms the relay and pulls through it; the button then un-manages a relayed board and disconnects a connected one (connect.modal_and_button)"),
    ("connect_fake_reconnect_platforms", "reconnect() reopens the same port with a DTR pulse or takes a fresh same-VID handle no other board holds, unless told not to search; on Windows it retries the close, reuses a port left open, and takes a USB connect event's port (connect.reconnect_platforms)"),
    ("connect_fake_wait_board_ready", "waitForBoardReady keeps one listener, probes WCB_WEBTOOL_CONFIG_PULL then <funcChar>backup every 10 s while connected and read, resolves on the end marker and gives up at its timeout (connect.wait_board_ready)"),
    ("connect_fake_send_collect_idle", "send() writes one at a time and refuses with no port; sendAndCollect returns on its sentinel or timeout; sendAndAwaitIdle returns 250 ms after the last line, or ok false at its hard cap (connect.send_collect_idle)"),
    ("connect_fake_line_handling", "The boot banner's function identifier, delimiter, command character and version go into the board's boot characters, config, General fields and card; RC heartbeat and stick lines reach no listener (connect.line_handling)"),
    ("hub_fake_protocol", "serial-hub.js: the tab with a port leads, a second tab follows (sends reach the port, bytes reach it) and takes over when the leader closes; an ambiguous match, a port that will not open or goes dead release the lock; releasePortKeepOpen keeps it open (hub.protocol)"),
    ("hub_fake_shared_connection", "A shared BoardConnection follows the hub's port state on its card, splits the hub's bytes into lines and sends through it; becomeDirect takes the borrowed port, becomeShared hands it to a new hub without reopening it and remembers the slot (hub.shared_connection)"),
    ("hub_fake_share_rebuild", "Share port tears down any shared connection, a shared relay's boards and card, and the old hub, then rebuilds it; a share no tab opens is torn down with a warning, off the hub and forgotten as the shared slot (hub.share_rebuild)"),
    ("hub_fake_flash_borrow", "A flash on the shared port this tab leads borrows it open (releasePortKeepOpen, becomeDirect), flashes direct and re-shares after the reconnect; no re-share when the board did not return or another board took the slot (hub.flash_borrow)"),
    ("pull_fake_slot_migration", "A board pulled in the wrong slot moves to its number with its terminal output, the old pane gone; if that slot is connected it stays where it landed with a warning and the other slot is untouched (pull.slot_migration)"),
    ("flasher_fake_image_choice", "The flasher's image set: an update with an unreadable layout stays app-only unless the app outgrew the legacy slot (refused); unreadable, blank or corrupted flash writes the full set; no bootloader files refuses a full flash; ascending order (flasher.image_choice)"),
    ("flasher_fake_fetch", "getBinaryData picks files by suffix, falls back to the legacy S3 bootloader for 16 MB, warns when the 8 MB one is missing (an 8 MB full flash then refused), and with no bootloader/partition files warns loudly and returns app only (flasher.fetch)"),
    ("flasher_fake_write_retry_reset", "A write failing once is retried; twice stops with the recovery steps when boot or partitions were written; progress is weighted per file; the board is reset by afterFlash or a DTR/RTS pulse and the port released (flasher.write_retry_reset)"),
    ("push_fake_post_flash_general", "The push after a flash or erase restores the pre-flash General fields and sends those, once; the next push reads General as it is (push.post_flash_general_restore)"),
    ("flash_fake_failure_recovery", "A failed flash reopens the same port; in the setup wizard a board already on the target firmware is pushed its config whole with the pre-flash General, another version told to retry; on Windows the port is closed (retried) first (flash.failure_recovery)"),
    ("flash_fake_factory_modal", "Factory Reset opens its modal for a USB board (refused relayed or disconnected); Erase NVS + Load Fresh Firmware runs boardGo in factory mode with the push-config box as ticked; Cancel runs nothing (flash.factory_modal)"),
    ("ota_fake_serial_stream", "An OTA over USB follows the board's cursor (a NAK rewinds), verifies with END, reconnects (direct) or waits on the shared port, and pulls; END failing or the cursor collapsing sends ABORT and restores a raised baud (ota.serial_stream)"),
    ("ota_fake_relay", "A wireless OTA through a relay frames BEGIN / 192-byte DATA / END by target and session following the target's cursor, re-arms the link and pulls with 12 attempts 8 s later; a non-zero status or collapsed cursor stops it with ABORT (ota.relay)"),
    ("ota_fake_edges_replay", "Once the last OTA ends, the ETM edges held during it are replayed one board at a time (pull, then terminal re-armed), skipping the OTA's target and a board with no relay; kept while another OTA runs (ota.suppressed_edges_replay)"),
    ("file_fake_load", "Load File sets General and a 'loaded file' baseline from the file, ETM and NaviCore included, so its own boards pulled afterwards open no modal; renders its boards, derives the controller, toasts; a sectionless text is refused, nothing changed; a failed parse toasts (file.load)"),
    ("wiz_fake_navigation", "The setup wizard: Next validates then saves, a card click advances 200 ms later once and not after Back, Review reads Apply & Connect, and leaving Firmware stamps each WCB slot factory or erase and no client slot (wiz.navigation)"),
    ("wiz_fake_save_steps", "Saving a setup-wizard step: an owner-named claimed label stored blank, a port a Maestro leaves (moved or removed) back to 9600 unless changed, ETM numbers with defaults for emptied ones, the Kyber ports, the NaviCore id (wiz.save_steps)"),
    ("wiz_fake_apply_config", "Apply writes the wizard's state onto the page: quantity and General, NaviCore on every WCB with Maestro boards remote, the whole Maestro table everywhere, a client slot as a client, ETM off, the controller and its id (wiz.apply_config)"),
)
for _key, _title in _FAKE_RESCAN_M:
    def _fake_rescan_m(bench, _id=f"wizard.{_key}"):
        run_wizard_test(bench, _id, device=None)
    test(f"wizard.{_key}", f"{_title} (no board: fake boards and ports in the page)")(_fake_rescan_m)


# The low-risk Wizard rows of the same re-scan (risk L, named at the end of each title): start-up, General and toasts
# (specs/init_general_rescan_l_fake.spec.js), the grid and the cards (specs/cards_rescan_l_fake.spec.js), the sequence
# and variable editors and the terminal (specs/editors_term_rescan_l_fake.spec.js), pulls, pushes, relay management
# and the flash/erase/OTA guards (specs/pull_push_rescan_l_fake.spec.js), the stats modal, the mesh panel and the RC
# panel (specs/mesh_rescan_l_fake.spec.js), the setup wizard (specs/wiz_rescan_l_fake.spec.js). No board and no real
# port, so CI runs them too. The batch's node rows (parser.tokens_misc, version.watch_script) are
# tests/wizard/unit/tokens_misc.test.js and watch_version.test.js, run by wizard.parser.
_FAKE_RESCAN_L = (
    ("init_fake_boot_steps", "The page starts with the splash open and the UI version in the footer; a start-up step that throws is logged and every later one still runs, the terminal's drag-to-resize included (init.boot_steps)"),
    ("init_fake_browser_warning", "A browser without Web Serial sees the warning banner, one with it never does (init.browser_warning)"),
    ("init_fake_theme_mode_prefs", "Advanced shows every advanced-only part and the ETM details only with ETM on; the mode and the light/dark theme survive a reload (init.theme_mode_prefs)"),
    ("init_fake_splash_choice", "The splash's Setup Wizard opens the guided setup on its welcome step; Config Tool only closes the splash (init.splash_choice)"),
    ("init_fake_stale_branch_warning", "A leftover wcb_fw_branch override warns until dismissed, above the splash; a /dev/<branch>/Wizard preview says nothing (init.stale_branch_warning)"),
    ("general_fake_derive_controller", "A pull or file load derives the Controller: CONTROLLER,ON,15 is NaviCore with id 15, neither is None, NaviCore wins over Kyber (general.derive_controller)"),
    ("general_fake_dirty_flag", "Typing in General marks it dirty and turns Push All amber; a pull or file load does not; Push All clears it (general.dirty_flag)"),
    ("general_fake_peerslive_badge", "A pulled PEERSLIVE shows 'mesh: N live peers' (singular for one); none hides the badge (general.peerslive_badge)"),
    ("toast_fake_ui", "Toasts escape their text, carry Copy and Dismiss, sit above an open modal; errors stay 12 s at least, duration 0 until dismissed (toast.ui)"),
    ("grid_fake_jump_nav", "The jump nav has a button per board with its alias, kept through a rebuild; a button scrolls to its board and the board most in view is highlighted (grid.jump_nav)"),
    ("grid_fake_add_section", "A section built for a slot with a config shows it, one for a connected slot comes up Connected, one built in Advanced shows its advanced parts (grid.add_section)"),
    ("grid_fake_slot_type", "The slot-type radio flips a pulled card to a client (pane, alias cut at 24, header) and back with the WCB config whole (grid.slot_type)"),
    ("identity_fake_alias", "The alias swaps the live delimiter for _ as well; the header and the nav follow the alias and the WCB number (identity.alias)"),
    ("identity_fake_sw_version", "After a pull the version is asked with <fc>version to 'End of Version' and shown; a boot banner's line sets it too (identity.sw_version)"),
    ("identity_fake_identify", "Identify sends <fc>IDENTIFY to a USB board and a one-chunk FRAG through a relay, with a toast; refused when not reachable (identity.identify)"),
    ("serial_fake_claim_ui", "Each claim locks its serial row with its note; a serial map and Maestro REMOTE's S1 are soft claims, left editable (serial.claim_ui)"),
    ("serial_fake_label_combobox", "A port label offers the device names as you type; arrows and Enter pick one into the config, the next label keeps its own list open, Escape closes, a locked label opens nothing (serial.label_combobox)"),
    ("kyber_fake_marc_port", "Moving the Marcduino port sets the new port to 9600 with broadcasts on, no ?KYBER line; the picker leaves out the Kyber port (kyber.marc_port)"),
    ("kyber_fake_targets_info", "The Kyber Targets panel lists every board's Maestros, a board pulled later included, or says none are configured (kyber.targets_info)"),
    ("kyber_fake_repush_warning", "A Maestro edit on another board reminds to re-push the Kyber board, once per 30 s (kyber.repush_warning)"),
    ("audio_fake_hcr_status_button", "The HCR Status button shows only with HCR configured and is enabled only while the board is on USB; through a relay it is disabled with a tooltip saying relay is not supported (audio.hcr_status_button)"),
    ("wled_fake_remote_rows", "A WLED hosted elsewhere is a read-only row, never pushed, and a live-control target (wled.remote_rows)"),
    ("mapping_fake_ghost_rows", "A port another board drives as PWM output is a locked ghost row, naming the source once it is loaded (mapping.ghost_rows)"),
    ("seq_fake_shared_indicator", "A sequence key other boards hold shows 'also on Wx', refreshed on typing, saving, removing and loading (seq.shared_indicator)"),
    ("seq_fake_buttons_counts", "TEST and SAVE need a connected or relayed board; the key and value counters follow the typing (seq.buttons_counts)"),
    ("var_fake_temporary_rows", "A temporary variable's REMOVE sends ?VAR,CLEAR (through a relay too) and leaves the config alone; an unset one sends nothing (var.temporary_rows)"),
    ("var_fake_refresh_list", "Refresh on a USB board reads ?VAR,LIST into the rows, the config and the baseline, keeps a value being edited, adds the board-only ones (var.refresh_list)"),
    ("term_fake_drawer", "The terminal drawer opens with the page padded and panes at their newest line, resizes between 120 px and 80%, Clear All empties every pane (term.drawer)"),
    ("term_fake_panes", "A pane's chip says USB or via WCB n, its input is disabled while unreachable, and a board moving slot takes its output along (term.panes)"),
    ("term_fake_send_history", "Terminal lines go into the board's own history, a repeat once; the arrows walk it (term.send_history)"),
    ("term_fake_log_routing", "A line goes to its board's pane when the board is connected, relayed or opened by hand; a merely discovered board's lands in the first pane (term.log_routing)"),
    ("pull_fake_wcbq_renders_floor", "A pulled WCB quantity above 1 renders that many sections; 1 changes nothing (pull.wcbq_renders_floor)"),
    ("pull_fake_success_state", "A pull's button, config, baseline, badge, toast and version request; an overlapping pull is dropped; only a manual pull says not connected (pull.success_state)"),
    ("pull_fake_remote_delegation", "Pull on a relayed board goes through the relay; a slot recorded as its own relay is refused; Retry pulls again (pull.remote_delegation)"),
    ("push_fake_progress_and_history", "A push shows 'Pushing... k/n', hides the bar after, gives the button back, releases the mesh hold and clears the controller history (push.progress_and_history)"),
    ("rpush_fake_guards", "A relayed push is refused with its reason when the relay is down, the board has no config, or no relay is set (rpush.guards)"),
    ("pushall_fake_exclusions", "Push All with nothing connected says 'No boards connected' and sends nothing (pushall.exclusions)"),
    ("remote_fake_manage_ui", "Managing through a relay enables Pull/Push/Identify/Stats, dims Factory Reset and Update FW with a tooltip; un-managing puts it back (remote.manage_ui)"),
    ("remote_fake_manage_one", "A relay card's 'Manage via relay' arms and pulls one board, never one on USB (remote.manage_one)"),
    ("remote_fake_relay_only_refusals", "Update FW, Factory Reset and OTA-over-USB are refused on a relayed board with a toast (remote.relay_only_refusals)"),
    ("rpull_fake_badges", "A remote pull shows Retrying, then Pull Failed with Retry after its last attempt; Retry and a pull that lands show Remote (rpull.badges)"),
    ("flash_fake_erase_no_reconnect", "An erase whose board never comes back says so, pushes and probes nothing, and gives the Push button back (flash.erase_path)"),
    ("flasher_fake_windows_baud", "The flasher loads at 460800 (ROM 115200), at 115200 on Windows (flasher.windows_baud)"),
    ("ota_fake_entry_guards", "The OTA button takes the relay path for a relayed board and USB otherwise; each refuses an unreachable board (ota.entry_guards)"),
    ("stats_fake_modal", "The ETM test shows only the last summary block; a relayed capture that times out shows what came; Refresh and errors (stats.modal)"),
    ("wdp_fake_render", "The mesh table's self row, capability names, Controller kind, peers count and toolbar, and the empty-mesh note (wdp.render)"),
    ("wdp_fake_refresh_no_board", "The mesh Refresh with no board says to connect one; a failed dump shows its error (wdp.refresh_no_board)"),
    ("wdp_fake_client_cards", "A client's card shows status and age, kind, controller, firmware and capability chips; an rc client feeds the RC panel (wdp.client_cards)"),
    ("rc_fake_discovery", "rc_hb/rc_trig/rc_mode lines make and update an RC card that goes 12 s after the last beat; a mesh-only RC shows telemetry off and goes after 40 s (rc.discovery)"),
    ("rc_fake_tool_url", "The RC Open link uses the saved config-tool URL; 'edit Config Tool URL' saves one, the default or empty removes it (rc.tool_url)"),
    ("hub_fake_rc_open_tool", "Open in Config Tool hands ?share=1 when leading, shares a direct board first unless another tab leads, else opens the plain URL with a hint (hub.rc_open_tool)"),
    ("wiz_fake_open_steps", "Opening the setup wizard makes a fresh password and MAC octets and the step list its answers need; closing stops watchers and detect jobs (wiz.open_steps)"),
    ("wiz_fake_validate_steps", "The wizard refuses no control system, a short password, a bad MAC octet, an id outside 1-20 (as its hints and inputs say) and two Maestros on one port (wiz.validate_steps)"),
    ("wiz_fake_step_html", "Every wizard step renders its controls from a two-board Kyber state: locked ports, S1/S2, Maestro ports, ETM with the firmware's defaults in its hints, review, firmware, connect rows (wiz.step_html)"),
    ("wiz_fake_connect_tip", "The Connect & Push tip names only buttons the row's port panel offers (Detect, + Authorize), no 'Use ->' (wiz.connect_tip_use_button)"),
    ("wiz_fake_watch_timeout", "The connect watcher gives up after 90 s with 'Timed out' and the Reconnect button (wiz.watch_push_verify)"),
    ("wiz_fake_status_mirror", "Push and flash progress is mirrored into the wizard's connect row while it is on screen (wiz.status_mirror)"),
)
for _key, _title in _FAKE_RESCAN_L:
    def _fake_rescan_l(bench, _id=f"wizard.{_key}"):
        run_wizard_test(bench, _id, device=None)
    test(f"wizard.{_key}", f"{_title} (no board: fake boards and ports in the page)")(_fake_rescan_l)


# WCB-WP21: the same on the bench, through the HIL bridge. W1 is handed to Chrome; W2 stays on its own USB, which is how
# the harness reads and restores it. Each writes only throwaway labels, sequences, a variable and a serial mapping, and
# ?HW with W1's own value, and puts back what it wrote in a finally before config_guard's check; the specs check their
# pushes' verbs before sending (tests/wizard/lib/wizard.js plannedVerbs).
def _w1_boots_noted(bench, test_id, run):
    """Run `run()` and note how many boot announces from W1 W2 heard meanwhile - three per boot - as a record of how
    often the test restarted W1 (the port handoff itself can reset it too, so it is noted, not judged)."""
    w2 = WCB(bench.dev("wcb2")) if bench.has("wcb2") else None
    m = w2.dev.mark() if w2 else None
    run()
    if w2:
        time.sleep(3.0)
        me = bench.usb_wcb_number()
        edges = [x for x in w2.dev.since(m) if x.startswith(f"[ETM] WCB{me} came ONLINE (boot)")]
        bench.note(f"{test_id}: W2 heard {len(edges)} boot announce(s) from W{me} (three per boot)")


@test("wizard.push_reboot_path", "A Wizard push that needs a reboot, on the shared port the first board gets: W1 reboots once (?HW with its own value, so nothing changes), the page stays connected and reads its boot, and a pull afterwards finds the config as it was", needs=["wcb1"])
def push_reboot_path(bench):
    """WCB-WP21 row 1. The plan's WCBQ+1 edit needs no reboot (D28: ?WCBQ applies live), so the push is ?HW with the
    board's own version - the reboot path with no config change. On this path the Wizard pulls 3 s after the boot
    banner (W-14, guarded by wizard.push_fake_shared_reboot_repull); the spec pulls once more and compares."""
    with config_guard(bench, 1):
        _w1_boots_noted(bench, "wizard.push_reboot_path", lambda: run_wizard_test(bench, "wizard.push_reboot_path"))


@test("wizard.push_reboot_path_direct", "The same on a direct connection: ?reboot, the port closed and reopened, the Wizard's own pull 3 s later lands whole and finds the config as it was, and the page ends connected", needs=["wcb1"])
def push_reboot_path_direct(bench):
    """WCB-WP21 row 1, the path the plan describes (boardGo closes the port and reconnects). The reconnect pulses DTR,
    which resets W1 once more (BoardConnection.reconnect), so the boot count is noted, not judged."""
    with config_guard(bench, 1):
        _w1_boots_noted(bench, "wizard.push_reboot_path_direct",
                        lambda: run_wizard_test(bench, "wizard.push_reboot_path_direct"))


@test("wizard.push_all_relay", "Push All with W1 on USB as the relay for W2: W2's label in one session before W1's own push, W1's reboot last, W1 back and pulled, and both labels land (then put back)", needs=["wcb1", "wcb2"])
def push_all_relay(bench):
    """WCB-WP21 row 2. The plan forced W1's reboot with a WCBQ edit, which reboots nothing (D28); ?HW with W1's own
    version does. W1 is connected direct, for Push All's close-and-reopen path; a relay on the shared port takes the
    shared branch (W-15), which wizard.push_fake_all_shared_relay guards."""
    w2 = WCB(bench.dev("wcb2"))
    l1, l2 = marker("L"), marker("M")
    with config_guard(bench, 1, 2) as before:
        try:
            _w1_boots_noted(bench, "wizard.push_all_relay", lambda: run_wizard_test(
                bench, "wizard.push_all_relay", args={"target": 2, "label1": l1, "label2": l2}))
            assert f"?LABEL,S5,{l1}" in snapshot(bench, 1), "W1's pushed label is not in its ?backup"
            assert f"?LABEL,S5,{l2}" in snapshot(bench, 2), "W2's label pushed through W1 is not in its ?backup"
        finally:
            for n, w in ((1, usb_wcb(bench)), (2, w2)):
                orig = token(before[n], "?LABEL,S5,")
                w.run(orig if orig else "?LABEL,CLEAR,S5")
            w2.run("?RTERM,STOP")               # a pull the Wizard completes starts W2's remote terminal (RAM only)


def _serial_mapped(w, port):
    return any(re.search(rf"Serial{port[1]} ->", x) for x in w.run("?MAP,SERIAL,LIST"))


@test("wizard.mapping_bidir_relay", "(should) The Wizard's mapping editor on W1 with W2 behind it: Save with bidir maps W1 S2 -> W2 S4 on W1 and the reverse on W2, lines flow both ways, and Remove clears both (W-21)", needs=["wcb1", "wcb2"])
def mapping_bidir_relay(bench):
    """WCB-WP21 row 3, serial half. The spec proves both mappings with probe lines; this checks what Remove leaves.
    The remote PWM destination half is left to wizard.editors_fake_mappings (no board): on the bench it would cost two
    PWM reboots of W1 for a clear the fake already shows is sent. W-21: Remove on W1 also clears W2's reverse half,
    through W1 (fixed 2026-10-04); this checks it on W2 itself."""
    s2, w2s4 = link(bench, 1, "S2"), link(bench, 2, "S4")
    w, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    if _serial_mapped(w, "S2") or _serial_mapped(w2, "S4"):
        raise Skip("W1 S2 or W2 S4 already has a serial mapping")
    s2.listen()
    w2s4.listen()
    with config_guard(bench, 1, 2):
        try:
            run_wizard_test(bench, "wizard.mapping_bidir_relay",
                            args={"target": 2, "m1": marker("A"), "m2": marker("B")})
            left1 = [t for t in snapshot(bench, 1) if t.upper().startswith("?MAP,SERIAL,S2,")]
            assert not left1, f"W1 kept its mapping after Remove: {left1}"
            left2 = [t for t in snapshot(bench, 2) if t.upper().startswith("?MAP,SERIAL,S4,")]
            assert not left2, f"W-21: Remove on W1 left the reverse mapping on W2: {left2}"
        finally:
            if _serial_mapped(w, "S2"):
                w.run("?MAP,SERIAL,CLEAR,S2")   # CLEAR puts back the flags the mapping overrode (tracker #46)
            if _serial_mapped(w2, "S4"):
                w2.run("?MAP,SERIAL,CLEAR,S4")
            w2.run("?RTERM,STOP")


@test("wizard.seq_var_editors", "The Wizard's sequence and variable editors on W1 and on W2 through it: a throwaway sequence saved, tested (W1 S1 sees it), renamed and removed, one over 198 characters saved to W2 in parts and read back exact; a variable set and cleared on each", needs=["wcb1", "wcb2"])
def seq_var_editors(bench):
    """WCB-WP21 row 4. Checked by ?SEQ,NAMES, ?SEQ,GET, ?MGMT,SEQ, ?MGMT,SEQGET and ?VAR,LIST through the page; the
    keys and the variable are this test's own and are cleared again here whatever the spec did."""
    link(bench, 1, "S1").listen()
    w, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    n = nonce()
    keys = {"key1": f"HILS{n}", "key2": f"HILR{n}", "key3": f"HILL{n}"}
    var = f"hilv{n}"
    with config_guard(bench, 1, 2):
        try:
            run_wizard_test(bench, "wizard.seq_var_editors",
                            args={"target": 2, **keys, "var": var, "m1": marker("S"), "m2": marker("T")})
        finally:
            for dev in (w, w2):
                listed = dev.run("?SEQ,NAMES")
                for k in keys.values():
                    if any(k in x for x in listed):
                        dev.run(f"?SEQ,CLEAR,{k}")
                if any(re.search(rf"^\s*{var}\s*=", x) for x in dev.run("?VAR,LIST")):
                    dev.run(f"?VAR,CLEAR,{var}")
            w2.run("?RTERM,STOP")


@test("wizard.wdp_da_forget", "A device announcing on W2 S4 shows in the W1-connected Wizard's mesh panel, and its Forget button removes it from W2 through W1", needs=["wcb1", "wcb2"])
def wdp_da_forget(bench):
    """WCB-WP21 row 5. The plan named W2 S3; S4 is the port s12's WDP-DA mesh tests use, unlabelled, with the same skip
    rules. The record is saved by its second announce (WCB_WDP.cpp), forgotten through the page, and forgotten here
    again whatever happened."""
    s4 = link(bench, 2, "S4")
    tokens = bench.config_tokens(2, refresh=True)
    if token(tokens, "?LABEL,S4,") or token(tokens, "?WDP,OFF"):
        raise Skip("W2 S4 is labelled or W2 has WDP off")
    name = "HILWF" + marker()[3:7]
    announce = f'@WDP1 {{"type":"{name}","fw":"1.0"}}\n'.encode()
    with config_guard(bench, 1, 2), Console(bench, 2) as c2:
        if c2.remote:
            raise Skip("W2 has no USB console here: its WDP-DA list cannot be read")
        _da_scrub_on(c2, "S4")
        try:
            prime(s4)
            time.sleep(0.3)
            m = c2.mark()
            s4.send(announce)
            c2.expect(rf"\[WDP-DA\] S4: {name} fw 1\.0", timeout=4, since=m)
            s4.send(announce)                     # the second announce saves it: only then is it advertised
            c2.expect(rf"\[WDP-DA\] S4: {name} saved", timeout=4, since=m)
            time.sleep(3)                         # W2's advert and device list reach W1
            run_wizard_test(bench, "wizard.wdp_da_forget", args={"target": 2, "name": name})
            assert name not in _da_types_on(c2, "S4"), "W2 still lists the device the Wizard forgot"
        finally:
            _da_forget_on(c2, "S4", name)


@test("wizard.relay_terminal", "W2's terminal pane in the Wizard, through W1: ;S2 typed there comes out of W2 S2, ?VERSION answers in that pane, and disconnecting W1 leaves W2 unmanaged", needs=["wcb1", "wcb2"])
def relay_terminal(bench):
    """WCB-WP21 row 6, for a WCB relay. Manage all and the MgmtRelay card are wizard.app_fake_relay_card (no board): the
    bench has no MgmtRelay (the old WCB19 is probe1)."""
    link(bench, 2, "S2").listen()
    w2 = WCB(bench.dev("wcb2"))
    with config_guard(bench, 1, 2):
        try:
            run_wizard_test(bench, "wizard.relay_terminal", args={"target": 2, "m1": marker("R")})
        finally:
            w2.run("?RTERM,STOP")
