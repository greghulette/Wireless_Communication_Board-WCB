// (should) INTELLEX.md finding 1 (IX-WP4): after the host flashes a WCB for the Wizard, the shim tries to tell the
// Wizard which build it wrote with `window.latestFirmwareVersion = st.version` (Intellex src/intellex_shim.js:1390). The
// Wizard's latestFirmwareVersion is a top-level let (Wizard/app.js:128), which a property on window cannot reach - the
// shim's own comment says so (:1176-1181) - and boardGo labels the flashed card from that let (app.js:7586-7587). So the
// card is labelled with whatever GitHub listed when the page loaded (nothing, offline), not with the build written.
// Nothing is flashed: /_api/flash-wcb and /_api/flash-status are fulfilled here, and the shim's replacement
// flashFirmware() is called directly, as boardGo calls it. Fails until the shim reaches the let.
const { test, expect, skipUnlessHost, hostGuard } = require('../lib/fixtures');

skipUnlessHost(test);

const BUILD = '6.2.1_250646RSEP2026_hilbench';           // the host's build tag: WCB_<tag>_ESP32.bin (wcb_flash.py:80)
const WIZARD_FORM = '6.2.1_250646RSEP2026';             // what app.js:174-176 would extract from the same file name

test('intellex.ui_latest_fw_version the Wizard learns the flashed build', async ({ page, context, rec }) => {
  const calls = await hostGuard(context, rec, {
    'flash-wcb': route => route.fulfill({ json: { ok: true, started: true, target: 'serial COMFAKE', appOnly: true,
                                                   eraseNvs: false } }),
  });
  await context.route(/\/_api\/flash-status(\?|$)/, route => route.fulfill({
    json: { running: false, ok: true, error: '', version: BUILD, percent: 100, log: ['Done — board is running ' + BUILD] },
  }));
  await page.goto('/wcb/Wizard/', { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some(l => l.includes('flashFirmware() now runs on the host')),
    { timeout: 15_000 }).toBe(true);
  const before = await page.evaluate(() => latestFirmwareVersion);            // eslint-disable-line no-undef
  const logs = await page.evaluate(async () => {
    const lines = [];
    await window.flashFirmware(null, null, { appOnly: true, onLog: l => lines.push(l) });
    return lines;
  });
  expect(calls.map(c => c.name), 'the replacement flashFirmware asked the host').toContain('flash-wcb');
  expect(calls.find(c => c.name === 'flash-wcb').body, 'Update FW is app-only').toEqual({ appOnly: true,
                                                                                           eraseNvs: false });
  expect(logs.some(l => l.includes(BUILD)), 'the flash log names the build').toBe(true);
  expect(await page.evaluate(() => window.latestFirmwareVersion), 'window.latestFirmwareVersion').toBe(BUILD);
  const after = await page.evaluate(() => latestFirmwareVersion);             // eslint-disable-line no-undef
  expect((after || '').replace(/^v/, ''),
    `the Wizard's own latestFirmwareVersion is still ${JSON.stringify(after)} (it was ${JSON.stringify(before)}) after ` +
    `the host wrote ${BUILD}: boardGo would label the card with that (INTELLEX.md finding 1)`).toMatch(
    new RegExp('^' + WIZARD_FORM.replace(/\./g, '\\.')));
});
