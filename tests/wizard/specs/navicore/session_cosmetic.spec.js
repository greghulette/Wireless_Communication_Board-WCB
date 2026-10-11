// Two small corners (docs/hil_plan/NAVICORE.md §2, nct.hub.share_param and nct.ui.cosmetic, L1): the tool opened by
// the WCB Wizard with ?share=1 joins the Wizard's shared port by itself as a follower, with no picker and no click
// (config_tool/index.html ~20925, connectSharedPort ~4735), and says what to do when there is no leader and the
// browser gives no port; and the cosmetics - the light / dark theme (~20938), the SBUS / PWM display unit (~3419,
// ~20894), the sidebar's collapse and drag resize and the terminal's drag resize (~6100-6215), and the Config window's
// remembered tab (~17335). nctool.shared_hub_two_pages covers the hub itself.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

async function secondTab(context) {
  const p = await context.newPage();
  p.setDefaultTimeout(15_000);
  p.ncErrors = [];
  p.on('pageerror', (e) => p.ncErrors.push(e.message));
  return p;
}
// A pointer drag from the middle of `handle` by (dx, dy).
async function drag(page, handle, dx, dy) {
  const b = await handle.boundingBox();
  expect(b, 'the handle is on screen').not.toBeNull();
  const x = b.x + b.width / 2, y = b.y + b.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx, y + dy, { steps: 4 });
  await page.mouse.up();
}

test.describe('opened by the WCB Wizard', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 1 } });

  test('nctool.share_param_autoconnect a tool opened with ?share=1 while another tab holds the WCB joins that port as a follower by itself: no picker, no click, the handshake goes through the owner, the config loads and the status says so', async ({ context, page, emu, serial }) => {
    await T.openTool(page);
    await T.connectViaWcb(page);
    const pings = emu.requests('PING').length;
    const b = await secondTab(context);
    await T.openTool(b, { path: T.TOOL_PATH + '?share=1' });
    await expect.poll(() => b.evaluate(() => [sharedActive, viaWcbActive, _configLoaded]), { timeout: 15_000 }).toEqual([true, true, true]);
    expect(serial.events.filter((e) => e.op === 'open').map((e) => e.pg), 'only the owner opened the port').toEqual([1]);
    expect(emu.requests('PING').length, 'the follower\'s handshake reached the board').toBeGreaterThan(pings);
    await expect(b.locator('#connect-modal')).not.toHaveClass(/open/);
    await expect(b.locator('#status-text')).toHaveText(/Connected/);
    expect(b.ncErrors).toEqual([]);
    await b.close();
    T.expectNoPageErrors(page);
  });
});

test.describe('opened by the WCB Wizard, with nothing to follow', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 1 }, serialOptions: { requestPort: 'cancel' } });

  test('nctool.share_param_no_leader with no tab holding a port and the browser granting none (no gesture), ?share=1 ends with a plain notice to click Connect and pick the tethered WCB, and nothing is sent', async ({ page, emu }) => {
    await T.openTool(page, { path: T.TOOL_PATH + '?share=1' });
    await expect.poll(() => T.termLines(page, /^· shared: no port selected — click Connect and choose the tethered WCB when the browser asks\.$/), { timeout: 15_000 }).toHaveLength(1);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No port selected. Click Connect again and choose your tethered WCB'));
    expect(await page.evaluate(() => [sharedActive, viaWcbActive, _configLoaded])).toEqual([false, false, false]);
    expect(emu.requests('PING')).toEqual([]);
    T.expectNoPageErrors(page);
  });
});

test.describe('the cosmetics', () => {
  test.use({ emuOptions: { config: BENCH, monitorHz: 10 } });   // the monitor stream feeds the live readout

  test('nctool.cosmetic_toggles the theme button flips light / dark, is remembered across a reload; the display unit prints the live values and the labels in SBUS or PWM microseconds and is remembered; the sidebar collapses and expands from its button, its handle resizes it (180-520 px); the terminal\'s handle resizes it; the Config window reopens on the tab last used', async ({ page, emu }) => {
    await T.openTool(page);
    // Theme.
    const theme = page.locator('#btn-theme');
    await expect(page.locator('body')).not.toHaveClass(/theme-light/);
    await expect(theme).toHaveText('☾');
    await theme.click();
    await expect(page.locator('body')).toHaveClass(/theme-light/);
    await expect(theme).toHaveText('☀');
    await expect(theme).toHaveAttribute('title', 'Switch to dark theme');
    expect(await page.evaluate(() => localStorage.getItem('atlasrc-theme'))).toBe('light');
    await T.openTool(page, { clock: false });
    await expect(page.locator('body')).toHaveClass(/theme-light/);
    await theme.click();
    await expect(page.locator('body')).not.toHaveClass(/theme-light/);
    expect(await page.evaluate(() => localStorage.getItem('atlasrc-theme'))).toBe('dark');
    // Display unit.
    await T.connectUsb(page, emu);
    await expect(page.locator('#live-ch6')).toHaveText('992');
    await expect(page.locator('.unit-label').first()).toHaveText('(SBUS)');
    await page.locator('input[name="unit-toggle"][value="us"]').check();
    expect(await page.evaluate(() => [displayUnit, localStorage.getItem('rcDisplayUnit')])).toEqual(['us', 'us']);
    await expect(page.locator('#live-ch6')).toHaveText('1500');
    await expect(page.locator('.unit-label').first()).toHaveText('(PWM)');
    await page.locator('input[name="unit-toggle"][value="sbus"]').check();
    await expect(page.locator('#live-ch6')).toHaveText('992');
    await expect(page.locator('.unit-label').first()).toHaveText('(SBUS)');
    expect(await page.evaluate(() => localStorage.getItem('rcDisplayUnit'))).toBe('sbus');
    // Sidebar.
    const panel = page.locator('#sidebar-panel'), toggle = page.locator('#sidebar-toggle'), handle = page.locator('#sidebar-handle');
    await expect(toggle).toHaveText('◀');
    await toggle.click();
    await expect(page.locator('body')).toHaveClass(/sidebar-collapsed/);
    await expect(panel).toHaveClass(/collapsed/);
    await expect(toggle).toHaveText('▶');
    await expect(toggle).toHaveAttribute('title', 'Expand sidebar');
    await toggle.click();
    await expect(page.locator('body')).not.toHaveClass(/sidebar-collapsed/);
    await expect(toggle).toHaveText('◀');
    const w0 = await panel.evaluate((e) => e.offsetWidth);
    await drag(page, handle, 60, 0);
    expect(await panel.evaluate((e) => e.offsetWidth)).toBe(Math.max(180, Math.min(520, w0 + 60)));
    await drag(page, handle, -2000, 0);
    expect(await panel.evaluate((e) => e.offsetWidth), 'clamped at 180').toBe(180);
    await drag(page, handle, 2000, 0);
    expect(await panel.evaluate((e) => e.offsetWidth), 'clamped at 520').toBe(520);
    // (The handle is off screen while the sidebar is collapsed, so onPointerMove's expand-on-drag branch has no UI path.)
    // Terminal.
    await page.locator('#btn-terminal').click();
    const term = page.locator('#terminal-panel');
    await expect(term).toHaveClass(/open/);
    const h0 = await term.evaluate((e) => e.offsetHeight);
    await drag(page, page.locator('#terminal-handle'), 0, -50);
    expect(await term.evaluate((e) => e.offsetHeight)).toBe(h0 + 50);
    await drag(page, page.locator('#terminal-handle'), 0, 2000);
    // The drag's clamp is 100 px in the inline style; the panel's own CSS keeps it a little taller than that.
    expect(await term.evaluate((e, h) => [e.style.height, e.offsetHeight < h], h0), 'clamped at 100').toEqual(['100px', true]);
    await page.locator('#terminal-collapse').click();
    // The Config window's last tab.
    await page.locator('#btn-hwsetup').click();
    await page.locator('#cfg-tabs .cfg-tab[data-tab="wcb"]').click();
    expect(await page.evaluate(() => localStorage.getItem('rcConfigLastTab'))).toBe('wcb');
    await page.evaluate(() => closeHwSetup());
    await page.locator('#btn-hwsetup').click();
    await expect(page.locator('#cfg-tabs .cfg-tab.active')).toHaveAttribute('data-tab', 'wcb');
    await page.evaluate(() => closeHwSetup());
    T.expectNoPageErrors(page);
  });
});
