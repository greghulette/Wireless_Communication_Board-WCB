// Fixtures for the NaviCore config tool's specs (specs/navicore/*.spec.js, ids nctool.*): a fresh browser context on
// the tool's own origin with a fake navigator.serial (lib/navicore/shim.js) wired to a device in Node. The device is
// the emulator (lib/navicore/emulator.js) unless the harness passed a pipe (L2, lib/navicore/pipe.js). The Wizard's
// fixtures (lib/fixtures.js) are untouched: those drive real Web Serial through persistent profiles.
//
// Hermetic by default: every request that leaves 127.0.0.1 is aborted and recorded in page.ncBlocked (Google Fonts,
// api.github.com, the esptool/crypto-js CDNs, the cheat-sheet / cloud-backup Worker). A spec that needs one of them
// mocks it with page.route / context.route, which take precedence (last registered wins). Nothing a spec does can
// reach GitHub's rate limit or the live cloud KV.
//
// Headless by default: no real port is involved, so nobody has to see Chrome. NCTOOL_HEADED=1 shows it.
const base = require('@playwright/test');
const { FakeSerial } = require('./shim');
const { NaviEmulator } = require('./emulator');
const { navicoreRoot } = require('./paths');
const hil = require('../hil');

// playwright.config.js: the second serve.js. NCTOOL_ORIGIN moves the L0-L2 specs to another port, for a second checkout
// (a git worktree) that must run its no-board specs while a bench run holds 8778/8779: a config that reuses an existing
// server would otherwise serve that run's tree. The L3 specs never take it (lib/navicore/webserial.js): Chrome keeps
// the NaviCore profile's Web Serial grant for 8779 only.
const NC_ORIGIN = process.env.NCTOOL_ORIGIN || 'http://127.0.0.1:8779';

const test = base.test.extend({
  // What the harness told this run; null when run standalone (same as the Wizard's fixture).
  hilCtx: async ({}, use) => {
    await use(hil.present ? await hil.context() : null);
  },

  // Per-spec emulator options: test.use({ emuOptions: { mode: 'via-wcb', ... } }).
  emuOptions: [{}, { option: true }],
  // Fake-port options (shim.js FakeSerial): requestPort 'grant' | 'cancel', granted, dummies, vid/pid.
  serialOptions: [{}, { option: true }],

  emu: async ({ emuOptions }, use) => {
    const emu = new NaviEmulator(emuOptions);
    await use(emu);
    emu.stop();
  },

  // The device behind the fake port: the emulator, or — when the harness piped a board (L2, hilCtx.pipe) — the real
  // one through the bridge (lib/navicore/pipe.js).
  device: async ({ emu, hilCtx }, use) => {
    if (hilCtx && hilCtx.pipe) {
      const { BridgePipe } = require('./pipe');
      const pipe = new BridgePipe({ device: hilCtx.device });
      await use(pipe);
      pipe.stop();
    } else {
      await use(emu);
    }
  },

  serial: async ({ device, serialOptions }, use) => {
    await use(new FakeSerial(device, serialOptions));
  },

  ncBrowser: [async ({}, use) => {
    const browser = await base.chromium.launch({ headless: !process.env.NCTOOL_HEADED });
    await use(browser);
    await browser.close();
  }, { scope: 'worker' }],

  context: async ({ ncBrowser, serial }, use, testInfo) => {
    testInfo.skip(!navicoreRoot(), 'the NaviCore repo is not beside this one (clone it, or set NAVICORE_REPO)');
    const context = await ncBrowser.newContext({ baseURL: NC_ORIGIN, viewport: { width: 1400, height: 900 } });
    context.ncBlocked = [];
    await context.route((url) => url.hostname !== '127.0.0.1', (route) => {
      context.ncBlocked.push(route.request().url());
      return route.abort('blockedbyclient');
    });
    await serial.install(context);
    await use(context);
    await context.close();
  },

  page: async ({ context }, use) => {
    const page = await context.newPage();
    page.setDefaultTimeout(15_000);   // a stuck click or wait fails in 15 s, not at the 240 s test timeout
    page.ncErrors = [];
    page.on('pageerror', (e) => page.ncErrors.push(e.message));
    await use(page);
  },

  // No first-run authorization happens here (playwright.config.js's 240 s is for that): 90 s per spec.
  ncTimeout: [async ({}, use, testInfo) => {
    if (testInfo.timeout > 90_000) testInfo.setTimeout(90_000);
    await use();
  }, { auto: true }],
});

module.exports = { test, expect: base.expect, hil, NC_ORIGIN };
