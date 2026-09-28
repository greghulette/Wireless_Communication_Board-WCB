// Playwright for the Wizard. Board tests are started one at a time by the HIL harness (tests/hil/hil/wizard.py),
// which hands the board's COM port to Chrome and passes HIL_BRIDGE; run standalone, only the no-board tests
// (wizard.smoke, wizard.kyber_*, wizard.remote_pull_fake_*) run.
//
// specs/navicore/ holds the NaviCore config tool's specs (nctool.*, docs/hil_plan/NAVICORE.md §5). They load the tool
// from the sibling NaviCore repo through serve.js's /NaviCore/ alias on their own origin (NC_ORIGIN below), behind a
// fake navigator.serial (lib/navicore/shim.js), and skip when the NaviCore repo is not beside this one.
const { defineConfig } = require('@playwright/test');

// The Web Serial grant is stored per origin, so this origin must never change or every profile needs
// re-authorizing. 8778, not 8777: that is the wizard-static launch config Greg keeps open.
const ORIGIN = 'http://127.0.0.1:8778';
// The NaviCore tool's origin: a second serve.js on 8779, so nothing a NaviCore spec does (localStorage, Web Locks,
// a future real-serial grant in .profiles/navicore) can land on the Wizard's 8778 origin. It serves the same tree, so
// the shared-hub spec still has the Wizard and the tool on one origin. Mirrored in lib/navicore/fixtures.js.
const NC_ORIGIN = 'http://127.0.0.1:8779';

module.exports = defineConfig({
  testDir: './specs',
  workers: 1,                // one board, one COM port, one Chrome profile at a time
  fullyParallel: false,
  retries: 0,
  timeout: 240_000,          // covers a first-run authorization, where Greg picks the port by hand
  reporter: 'line',
  use: { baseURL: ORIGIN },
  webServer: [
    {
      // Our own tiny static server (serve.js): the repo root, not Wizard/, because the page loads ../Images/*.
      // Node, not `python -m http.server`, so a CI runner without `python` on PATH works the same as this PC.
      command: 'node serve.js 8778',
      url: `${ORIGIN}/Wizard/index.html`,
      reuseExistingServer: true,
      timeout: 20_000,
    },
    {
      // Ready on the Wizard page, which is always there: the NaviCore alias may not be (CI, no sibling checkout).
      command: 'node serve.js 8779',
      url: `${NC_ORIGIN}/Wizard/index.html`,
      reuseExistingServer: true,
      timeout: 20_000,
    },
  ],
});
