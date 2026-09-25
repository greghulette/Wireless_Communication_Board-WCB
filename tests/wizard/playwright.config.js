// Playwright for the Wizard. Board tests are started one at a time by the HIL harness (tests/hil/hil/wizard.py),
// which hands the board's COM port to Chrome and passes HIL_BRIDGE; run standalone, only the no-board tests
// (wizard.smoke, wizard.kyber_*, wizard.remote_pull_fake_*) run.
const { defineConfig } = require('@playwright/test');

// The Web Serial grant is stored per origin, so this origin must never change or every profile needs
// re-authorizing. 8778, not 8777: that is the wizard-static launch config Greg keeps open.
const ORIGIN = 'http://127.0.0.1:8778';

module.exports = defineConfig({
  testDir: './specs',
  workers: 1,                // one board, one COM port, one Chrome profile at a time
  fullyParallel: false,
  retries: 0,
  timeout: 240_000,          // covers a first-run authorization, where Greg picks the port by hand
  reporter: 'line',
  use: { baseURL: ORIGIN },
  webServer: {
    // Our own tiny static server (serve.js): the repo root, not Wizard/, because the page loads ../Images/*.
    // Node, not `python -m http.server`, so a CI runner without `python` on PATH works the same as this PC.
    command: 'node serve.js 8778',
    url: `${ORIGIN}/Wizard/index.html`,
    reuseExistingServer: true,
    timeout: 20_000,
  },
});
