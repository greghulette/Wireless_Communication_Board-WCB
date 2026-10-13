// Helpers shared by the medium-risk re-scan specs (specs/*_rescan_fake.spec.js, 2026-10-12): the page set up with the
// fake-board recorder (lib/fake.js), the verify pulls held back, and the toasts read since a mark.
const { openWizard } = require('./wizard');
const { install, pullFake, push } = require('./fake');

async function setup(page) {
  await openWizard(page);
  await install(page);
}

// boardGo pulls the board again 300 ms after a push that needs no reboot (and boardPull runs on other timers too). A
// fake answers that pull with the backup it was given, not with what was pushed, so a spec that pushes more than once
// holds those pulls back: each push is then judged against the pulled baseline alone. The page's own calls look
// boardPull up by name, so the wrapper sees them all; pull() below is a pull the spec asks for, which always runs.
const holdVerifyPulls = (page) => page.evaluate(() => {
  if (!window.__realBoardPull) {
    window.__realBoardPull = boardPull;
    window.boardPull = (...a) => (window.__pullsHeld ? Promise.resolve() : window.__realBoardPull(...a));
  }
  window.__pullsHeld = true;
});
async function pull(page, slot, tokens, opts) {
  await page.evaluate(() => { window.__heldBefore = !!window.__pullsHeld; window.__pullsHeld = false; });
  await pullFake(page, slot, tokens, opts);
  await page.evaluate(() => { window.__pullsHeld = window.__heldBefore; });
}

const toastMark = (page) => page.evaluate(() => __fake.toasts.length);
const toastsSince = (page, m) => page.evaluate((m) => __fake.toasts.slice(m).map((t) => t.message).join('\n'), m);
// A select's option values, and whether an element is shown (its own display style).
const options = (page, id) => page.evaluate((id) => [...document.getElementById(id).options].map((o) => o.value), id);
const shown = (page, id) => page.evaluate((id) => document.getElementById(id).style.display !== 'none', id);
// What a push of slot n sends, with the reboot left out (boardGo's skipReboot), as 'n:command'.
const sent = async (page, n) => (await push(page, n, { skipReboot: true })).sent;

module.exports = { setup, holdVerifyPulls, pull, toastMark, toastsSince, options, shown, sent };
