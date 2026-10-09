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
