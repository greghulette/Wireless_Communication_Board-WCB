// Every test gets the page from a PERSISTENT Chrome profile, because that is where Chrome keeps Web Serial grants:
// authorize a board once and later runs reach it through getPorts() with no dialog. One profile per bench device
// (.profiles/<device>), holding only that board's grant — Web Serial shows no COM numbers, and wcb2 and both
// probes are identical CP210x bridges, so a shared profile could not tell them apart.
const path = require('path');
const base = require('@playwright/test');
const hil = require('./hil');

const test = base.test.extend({
  // What the harness told this run (device, com, wcb, vid, pid, args); null when run standalone.
  hilCtx: async ({}, use) => {
    await use(hil.present ? await hil.context() : null);
  },

  context: async ({ hilCtx, baseURL }, use) => {
    const profile = path.join(__dirname, '..', '.profiles', hilCtx?.device || 'no-board');
    const context = await base.chromium.launchPersistentContext(profile, {
      // Headed by default: a first authorization needs Chrome's port dialog on screen, and Web Serial in
      // headless Chrome is untested here. WIZ_HEADLESS=1 to try it once every profile is authorized.
      headless: !!process.env.WIZ_HEADLESS,
      baseURL,
      viewport: { width: 1400, height: 900 },
    });
    // The Wizard asks the GitHub API for the firmware list at every start (fetchLatestFirmwareVersion), and so did
    // every spec's page: a run of a few hundred spent the 60 calls an hour GitHub allows a computer without a sign-in,
    // and whatever really needs GitHub later in the run (Intellex's online checks, a push's CI watch) met HTTP 403.
    // An empty list here, which the Wizard reads as "no firmware listed". A spec that mocks GitHub itself
    // (page.route, or a later context.route) takes precedence.
    await context.route('https://api.github.com/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
    await use(context);
    await context.close();   // closes the Web Serial port, so the harness can take it back
  },

  page: async ({ context }, use) => {
    const page = context.pages()[0] || await context.newPage();
    page.wizErrors = [];
    page.on('pageerror', (e) => page.wizErrors.push(e.message));
    await use(page);
  },
});

module.exports = { test, expect: base.expect, hil };
