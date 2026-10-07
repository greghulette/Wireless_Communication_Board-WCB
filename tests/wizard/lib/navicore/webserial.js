// The L3 fixtures (docs/hil_plan/NAVICORE.md §5.1 and NC-WP13): the NaviCore config tool over REAL Web Serial, in a
// persistent Chrome profile, tests/wizard/.profiles/navicore, that holds only NaviCore's grant - the Wizard's pattern
// (lib/fixtures.js), on the tool's own origin. Chrome stores a grant per origin, so that origin never moves: 8779,
// whatever NCTOOL_ORIGIN says (that override is for the fake-port layers only, lib/navicore/fixtures.js).
//
// Only under the harness with NaviCore handed to Chrome (hil/wizard.py run_wizard_test(..., device="navicore"), opt-in
// navicore_webserial). Anywhere else the context fixture skips before Chrome starts, so a standalone or CI run opens
// no window and makes no profile.
//
// Headed: the first run for the profile needs someone at Chrome's port dialog (authorize below). Hermetic like the
// fake-port layers: every request that leaves 127.0.0.1 is aborted and recorded unless a spec routes it itself (the
// flash spec serves GitHub's listing and esptool-js/CryptoJS from this repo).
const path = require('node:path');
const base = require('@playwright/test');
const hil = require('../hil');
const { navicoreRoot } = require('./paths');

const NC_ORIGIN = 'http://127.0.0.1:8779';
const PROFILE = path.join(__dirname, '..', '..', '.profiles', 'navicore');

const test = base.test.extend({
  hilCtx: async ({}, use) => {
    await use(hil.present ? await hil.context() : null);
  },

  context: async ({ hilCtx }, use, testInfo) => {
    testInfo.skip(!hilCtx || hilCtx.pipe || hilCtx.device !== 'navicore',
                  'L3 runs under the HIL harness with NaviCore handed to Chrome: python tests/hil/run.py "nctool.webserial_*" ' +
                  '(attended, opt-in navicore_webserial)');
    testInfo.skip(!navicoreRoot(), 'the NaviCore repo is not beside this one (clone it, or set NAVICORE_REPO)');
    const context = await base.chromium.launchPersistentContext(PROFILE, {
      headless: false, baseURL: NC_ORIGIN, viewport: { width: 1400, height: 900 },
    });
    context.ncBlocked = [];
    await context.route((url) => url.hostname !== '127.0.0.1', (route) => {
      context.ncBlocked.push(route.request().url());
      return route.abort('blockedbyclient');
    });
    await use(context);
    await context.close();   // closes the Web Serial port, so the harness can take COM5 back
  },

  page: async ({ context }, use) => {
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(15_000);
    page.ncErrors = [];
    page.on('pageerror', (e) => page.ncErrors.push(e.message));
    await use(page);
  },
});

// Indices, in navigator.serial.getPorts() order, of the granted ports whose USB ids are the harness's device's.
function grantedMatches(page, ctx) {
  return page.evaluate(async ({ vid, pid }) => (await navigator.serial.getPorts())
    .map((p, k) => { const i = p.getInfo(); return i.usbVendorId === vid && i.usbProductId === pid ? k : -1; })
    .filter((k) => k >= 0), { vid: ctx.vid, pid: ctx.pid });
}

// First run for the profile: a button in the page, which someone clicks (as lib/wizard.js explains: a scripted click
// opened a dialog a Mac closed at once), opens Chrome's port dialog filtered to NaviCore's USB ids, and they pick the
// COM port the harness names; a dialog that closes leaves the button to click again. Needing
// it at all means nobody has authorized NaviCore in this profile, so a cancel or no answer in 3 minutes SKIPS (as
// lib/wizard.js does); WIZ_NO_AUTHORIZE=1 skips without opening the dialog. The SBUS controller is the same ESP32-S3
// USB-Serial/JTAG (303A:1001), so the dialog may list it too: its grant would do no harm (the harness holds its port,
// so it cannot be opened), and the PONG the connect waits for is what proves the port is NaviCore.
async function authorize(page, ctx) {
  if (process.env.WIZ_NO_AUTHORIZE) {
    test.skip(true, `NaviCore (${ctx.com}) is not authorized in .profiles/navicore and WIZ_NO_AUTHORIZE is set - run ` +
                    'an nctool.webserial_* test once with someone at the keyboard to authorize it');
  }
  const say = `click here, then pick ${ctx.com} (NaviCore) in Chrome's port dialog - once per profile`;
  console.log(`>>> ${say}`);
  await page.evaluate(({ say, vid, pid }) => {
    const b = document.createElement('button');
    b.id = 'hil-authorize';
    b.textContent = `HIL: ${say}`;
    b.style.cssText = 'position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:99999;' +
                      'padding:10px 16px;font:bold 14px sans-serif;background:#bf8700;color:#fff;border:0;border-radius:6px';
    b.onclick = async () => {
      try {
        const p = await navigator.serial.requestPort({ filters: [{ usbVendorId: vid, usbProductId: pid }] });
        const i = p.getInfo();
        b.dataset.done = i.usbVendorId === vid && i.usbProductId === pid ? 'ok' : 'wrong port';
      } catch (e) {
        b.dataset.closed = String(Number(b.dataset.closed || 0) + 1);
        b.textContent = `HIL: the port dialog closed (${e.name}) - ${say}`;
      }
    };
    document.body.appendChild(b);
  }, { say, vid: ctx.vid, pid: ctx.pid });
  await page.bringToFront();
  let done;
  try {
    done = await (await page.waitForFunction(() => document.getElementById('hil-authorize')?.dataset.done, null,
                                             { timeout: 180_000 })).jsonValue();
  } catch {
    done = 'nobody answered the port dialog within 3 minutes';
  }
  await page.evaluate(() => document.getElementById('hil-authorize')?.remove()).catch(() => {});
  if (done !== 'ok') test.skip(true, `NaviCore (${ctx.com}) was not authorized: ${done}`);
}

// Connect the tool to the real NaviCore over its granted port, the way "Connect via USB" does once the chooser has
// answered: the tool's own openPortAndStart(port, 4000) (index.html:4511-4605; connectDirect :4639-4645 only adds the
// chooser, which is Chrome's native dialog and would need a person on every run). The real 4 s settle is kept - the
// board restarts on the open and boots inside it (:4482-4491). Each granted port with NaviCore's USB ids is tried in
// turn; one the OS will not open (the SBUS controller's, which the harness holds) throws and is skipped. -> the index
// of the port that answered, or throws.
async function connectGranted(page, ctx) {
  let matches = await grantedMatches(page, ctx);
  if (!matches.length) {
    await authorize(page, ctx);
    matches = await grantedMatches(page, ctx);
  }
  if (!matches.length) throw new Error(`no granted port in .profiles/navicore has NaviCore's USB ids`);
  const tried = [];
  for (const k of matches) {
    const r = await page.evaluate(async (k) => {
      const p = (await navigator.serial.getPorts())[k];
      try { return { pong: await openPortAndStart(p, 4000, true, false) }; } catch (e) { return { error: `${e.name}: ${e.message}` }; }
    }, k);
    if (r.pong) return k;
    tried.push(r.error || 'opened, no PONG');
    await page.evaluate(() => (port ? disconnect() : null)).catch(() => {});
  }
  throw new Error(`no granted port answered as NaviCore: ${tried.join('; ')}`);
}

module.exports = { test, expect: base.expect, hil, NC_ORIGIN, PROFILE, grantedMatches, authorize, connectGranted };
