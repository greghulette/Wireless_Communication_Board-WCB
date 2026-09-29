// L3: the config tool over REAL Web Serial on NaviCore's own port (docs/hil_plan/NAVICORE.md §5.1 and NC-WP13), in the
// persistent profile tests/wizard/.profiles/navicore (lib/navicore/webserial.js). Attended: the first run asks someone
// to pick NaviCore's COM port in Chrome's dialog, and the flash spec rewrites NaviCore's firmware. Only under the harness
// with NaviCore handed to Chrome, inside nc_guard: python tests/hil/run.py "nctool.webserial_*" with bench.json opt-in
// navicore_webserial. Anywhere else they skip before Chrome starts.
//
// As in every NaviCore spec, nothing here reads a config value or the terminal wholesale: the CONFIG echo and the boot
// banner's SoftAP line are in the terminal pane, and T.termLines only returns the lines a pattern picks (never CONFIG).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect, hil, connectGranted } = require('../../lib/navicore/webserial');
const T = require('../../lib/navicore/tool');
const F = require('../../lib/navicore/firmware');
const { navicoreRoot, ROOT } = require('../../lib/navicore/paths');

// A fresh page every time: the persistent profile keeps localStorage between runs (the tool keeps a unit, the last
// tab, a custom command library there), and the cache is off as openWizard's is. No fake clock: the real board resets
// on the open and needs the tool's real 4 s settle to boot inside (index.html:4482-4491, :4555).
async function openFresh(page) {
  await T.openTool(page, { clock: false });
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.waitForFunction(() => typeof openPortAndStart === 'function' && typeof handleBoardMessage === 'function');
  await page.evaluate(() => _cmdlibReady);
}

// nct.conn.direct over the real transport: Chrome asserts DTR/RTS inside open() and NaviCore's native USB resets the
// chip on that edge, before the tool can deassert them (index.html:4482-4491, :4528-4540). The tool's 4 s settle must
// cover the boot, so the direct probe answers and the tool neither misdetects a WCB nor sees a phantom diff. The
// harness proves the restart by NaviCore's uptime; the spec notes whether the boot banner reached the terminal.
test('nctool.webserial_connect_reset over real Web Serial the open restarts NaviCore, and the connect still succeeds on the direct link: its PONG version, its CONFIG applied with nothing to save, no switch to Via WCB', async ({ page, hilCtx }) => {
  test.setTimeout(420_000);                                           // a first run waits up to 3 min for the port dialog
  const a = hilCtx.args;
  await openFresh(page);
  await connectGranted(page, hilCtx);
  await page.waitForFunction(() => _configLoaded === true && isMonitoring === true, null, { timeout: 30_000 });
  expect(await T.state(page)).toMatchObject({ viaWcbActive: false, connected: true, status: 'Connected ✓' });
  await expect(page.locator('#fw-current-version')).toHaveText(a.version);
  expect(await T.toasts(page)).not.toContainEqual(expect.stringContaining('switched to Via WCB'));
  expect(await page.evaluate(() => Object.keys(_diffConfigBranches(config, _configBaseline))),
         'branches a Save right after connecting would send').toEqual([]);
  const banner = (await T.termLines(page, /Reset reason: \d+|setup complete\./)).length;
  await hil.note(`webserial_connect_reset: ${banner} boot-banner line(s) in the tool's terminal after the open`);
  await page.locator('#btn-connect').click();                         // Disconnect
  await page.waitForFunction(() => !port);
  T.expectNoPageErrors(page);
});

// The flash set the harness vouched for: the bench image as the app (the one NaviCore runs), the partition table the
// bench build made (byte-identical to the one NaviCore publishes for every user flash, or the spec skips), and the
// NaviCore repo's custom bootloader - never the build's stock one (flasher.js:193-202).
function flashSet(a) {
  const fw = path.join(navicoreRoot(), 'firmware');
  const published = fs.readdirSync(fw).filter((n) => /^NaviCore_.+_ESP32S3_part\.bin$/.test(n));
  const part = fs.readFileSync(a.part);
  if (!published.length || !published.every((n) => fs.readFileSync(path.join(fw, n)).equals(part))) {
    test.skip(true, `the bench build's partition table is not the one NaviCore publishes (${published.join(', ') || 'none'} ` +
                    'in firmware/): the flash would write a table the board may not hold');
  }
  return {
    version: a.version,
    app: fs.readFileSync(a.image),
    part,
    boot: fs.readFileSync(path.join(fw, F.BOOT_NAME)),
    names: { app: `NaviCore_${a.version}_ESP32S3.bin`, part: `NaviCore_${a.version}_ESP32S3_part.bin`, boot: F.BOOT_NAME },
  };
}

// esptool-js 0.4.7 and CryptoJS 4.2.0 from this repo's own copies (Wizard/vendor, the self-contained bundles the Wizard
// flashes with), served at the CDN URLs the tool imports (flasher.js:26-27): the real library, nothing fetched.
async function serveFlashTools(page) {
  const vendor = path.join(ROOT, 'Wizard', 'vendor');
  await page.route((u) => u.href === F.ESPTOOL_URL, (route) => route.fulfill({
    status: 200, headers: { 'access-control-allow-origin': '*' }, contentType: 'text/javascript',
    body: fs.readFileSync(path.join(vendor, 'esptool-js', 'esptool-js-0.4.7.bundle.js'), 'utf8'),
  }));
  await page.route((u) => u.href === F.CRYPTOJS_URL, (route) => route.fulfill({
    status: 200, headers: { 'access-control-allow-origin': '*' }, contentType: 'text/javascript',
    body: fs.readFileSync(path.join(vendor, 'crypto-js', 'crypto-js-4.2.0.min.js'), 'utf8'),
  }));
}

// nct.fw.flash_update and nct.fw.wipe_misleading on the board: "⚠ Full Wipe & Flash" from a live session reuses the
// port (runFirmwareFlash, index.html:17857-18007), esptool-js writes otadata and NVS as 0xFF and then the bootloader,
// the table and the app (flasher.js:267-423), resets the chip and the tool reconnects (reopenAfterFlash). The texts
// promise the saved config is erased (:17957 and the confirm at :18124-18127, D-NC34); the harness proves it was not.
test('nctool.webserial_flash_same_image the tool\'s Full Wipe & Flash over real Web Serial and esptool-js writes the custom bootloader, the partition table and the bench image, reconnects on its own, and the reloaded config is the one it had before the wipe', async ({ page, hilCtx }) => {
  test.setTimeout(900_000);
  const a = hilCtx.args;
  const set = flashSet(a);
  await F.mockFirmware(page, set, { decoys: false });                 // GitHub's firmware/ listing: the bench set only
  await serveFlashTools(page);                                        // routed after mockFirmware: the last route wins
  const dialogs = T.answerDialogs(page, [true]);
  await openFresh(page);
  await connectGranted(page, hilCtx);
  await page.waitForFunction(() => _configLoaded === true && isMonitoring === true, null, { timeout: 30_000 });
  // Kept in the page (it holds the passwords): after the renders, as it will be after the reconnect.
  await page.evaluate(() => { window.__hilBefore = _stringifyStable(config); });
  await page.locator('#btn-hwsetup').click();
  await page.locator('#cfg-tabs .cfg-tab[data-tab="firmware"]').click();
  await expect(page.locator('#fw-latest-version')).toHaveText(a.version, { timeout: 15_000 });
  await page.locator('#btn-fw-wipe').click();
  expect(dialogs.map((d) => d.type), 'the Full Wipe confirm').toEqual(['confirm']);
  await expect(page.locator('#fw-status')).toHaveText(/^(Flash complete — reconnected\.|Flash complete — click Connect to resume\.|Flash failed\.)$/,
                                                      { timeout: 600_000 });
  const log = await page.locator('#fw-log').textContent();
  await expect(page.locator('#fw-status'), 'the flash and the reconnect').toHaveText('Flash complete — reconnected.');
  for (const t of ['⚠ Full wipe mode — NVS and OTA data will be erased.', 'Reusing the connected port for flashing',
                   'Loaded ', '(boot + partitions + app)', 'NVS (0x9000, 20 KB) and OTA data (0xE000, 8 KB) will be erased.',
                   'Reconnected — config session resumed.']) {
    expect(log, `the flash log says "${t}"`).toContain(t);
  }
  await page.waitForFunction(() => _configLoaded === true, null, { timeout: 30_000 });
  await expect(page.locator('#fw-current-version')).toHaveText(a.version);
  expect(await page.evaluate(() => _stringifyStable(config) === window.__hilBefore),
         'the config the tool reloaded after the wipe is the one it had before').toBe(true);
  await page.evaluate(() => closeHwSetup());
  await page.locator('#btn-connect').click();
  await page.waitForFunction(() => !port);
  T.expectNoPageErrors(page);
});
