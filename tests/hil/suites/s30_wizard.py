"""The Wizard (browser config tool) against a real board over Web Serial. Each test hands W1's USB port to Chrome,
runs the matching Playwright test from tests/wizard/specs (same id), and takes the port back — the harness decides
what the board should look like and checks what it looks like afterwards; the browser test only drives the page.
See docs/HIL_TESTING.md § Wizard tests."""
from hil.runner import test
from hil.wizard import run_unit_tests, run_wizard_test
from suites.common import config_guard, link, marker, snapshot, token, usb_wcb


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
