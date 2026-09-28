// L2: the config tool against the REAL NaviCore, through the harness's own COM handle (the pipe: lib/navicore/pipe.js,
// hil/bridge.py /serial/*; docs/hil_plan/NAVICORE.md §5.2, NC-WP13). Only under the harness, which runs each inside
// nc_guard: python tests/hil/run.py nctool.board_connect_config. Standalone and in CI these skip.
//
// Nothing here may leak a credential: the CONFIG line carries the mesh and AP passwords, so a spec reads counts,
// booleans and key names only, never a value, and never the terminal pane.
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

test.beforeEach(({ hilCtx }) => {
  test.skip(!hilCtx || !hilCtx.pipe, 'L2 runs under the HIL harness with NaviCore piped: python tests/hil/run.py "nctool.board_*"');
});

test('nctool.board_connect_config the tool connects to the real NaviCore through the pipe (no reset: the harness holds the port), applies its CONFIG, and a Save right after sends nothing', async ({ page, device }) => {
  await T.openTool(page);
  await page.locator('#btn-connect').click();
  await page.locator('#connect-modal .connect-opt').first().click();
  await page.waitForFunction(() => window.__hilSerial.isOpen());
  await page.clock.fastForward(4000);
  await page.waitForFunction(() => _configLoaded === true && isMonitoring === true, null, { timeout: 45_000 });
  const s = await T.state(page);
  expect(s).toMatchObject({ viaWcbActive: false, connected: true, status: 'Connected ✓' });
  await expect(page.locator('#fw-current-version')).toHaveText(/^v\d+\.\d+\.\d+_\w+$/);
  const facts = await page.evaluate(() => ({
    deviceId: config.wcbNetwork.deviceId, mappings: Object.keys(config.mappings).length,
    baseline: !!_configBaseline, diff: Object.keys(_diffConfigBranches(config, _configBaseline)),
  }));
  expect(facts.baseline).toBe(true);
  expect(facts.deviceId).toBe(20);
  expect(facts.mappings).toBeGreaterThan(0);
  // A phantom diff would make the next Save WRITE the droid's config: report its branch names and never press Save.
  expect(facts.diff, 'branches a Save right after connecting would send').toEqual([]);
  await page.locator('#btn-hwsetup').click();
  await expect(page.locator('#push-budget')).toHaveText('No changes to save');
  await page.locator('#btn-hwsetup-save').click();
  await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No changes to save'));
  // Disconnect as a user does, so NaviCore stops streaming PWM_UPDATE (the tool's START_MONITOR has no timeout).
  await page.evaluate(() => closeHwSetup());
  await page.locator('#btn-connect').click();
  await page.waitForFunction(() => !port);
  await device.flush();
  expect(device.sentTypes.filter((t) => t === 'SET_CONFIG' || t === 'RESET_DEFAULTS')).toEqual([]);
  expect(device.sentTypes).toEqual(expect.arrayContaining(['PING', 'GET_CONFIG', 'START_MONITOR', 'STOP_MONITOR']));
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});
