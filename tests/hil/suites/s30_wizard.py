"""The Wizard (browser config tool) against a real board over Web Serial. Each test hands W1's USB port to Chrome,
runs the matching Playwright test from tests/wizard/specs (same id), and takes the port back — the harness decides
what the board should look like and checks what it looks like afterwards; the browser test only drives the page.
See docs/HIL_TESTING.md § Wizard tests."""
from hil.runner import test
from hil.wcb import PULL_MAX, WCB, group_tokens
from hil.wizard import run_unit_tests, run_wizard_test
from suites.common import config_guard, link, marker, snapshot, token, usb_wcb
from suites.s03_wcb import SEQ_ROW, _clear, _factory_reply, _grow, _grow_over


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
