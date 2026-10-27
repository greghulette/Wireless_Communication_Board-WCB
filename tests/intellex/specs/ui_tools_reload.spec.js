// An open tool page reloads itself onto a newer copy of its tool - and only once it has nothing to lose (Intellex 0.2.7,
// "reload when safe"). intellex.ui_tools_reload_when_safe.
//
// Greg, 2026-10-26: "It doesn't make sense to have to manually refresh the page if we update them." The host fetches a
// newer published tool by itself (host.py _auto_update_tools_loop; intellex.tools_auto_update_unit) and /_api/status
// `tools` names the copy now served (_bundle_token). intellex_shim.js onToolsStatus compares that with the copy the page
// loaded and reloads it when pageBusy finds nothing a reload would lose; meanwhile it says what it waits for.
// tools/smoke_tools_reload.js checks that logic against a fake page, each state alone. This runs it in the real tools,
// in a browser, through a staged host: the tools' own globals must read as "nothing to lose" when nothing is - a renamed
// variable would read as busy forever and the page would never reload - and the token must move when a copy lands.
//
// A copy "lands" through the harness's swap_tool hook: the tool's whole directory copied and renamed into place, as
// fetch_webui.py _swap_in does, so nothing but the directory's identity changes. Checked, for each tool:
//   - an idle page reloads within a few seconds, with no notice and no page error;
//   - the config tool: an open window (Mesh stats, the tool's own openMeshStats) holds it and the notice says so, then
//     closing it reloads the page; a key just pressed holds it until TYPING_QUIET_MS (15 s) has passed;
//   - the Wizard: unpushed general settings (generalSettingsDirty, which its own Push All reads) hold it, and the
//     notice names the WCB Wizard; once they are pushed (false again) it reloads.
// A reload is told apart by a mark set on `window` beforehand, which only the page that loaded keeps.
const { test, expect, skipUnlessHost, hostGuard } = require('../lib/fixtures');
const { hil, hostGet } = require('../lib/board');

skipUnlessHost(test);

const WATCH_MS = 2000;                 // intellex_shim.js setInterval(watchLink, 2000)

// Loaded, and the shim's first /_api/status recorded as the copy the page loaded: two of its polls.
async function settle(page, path) {
  await page.goto(path, { waitUntil: 'load' });
  await page.waitForTimeout(2 * WATCH_MS + 500);
  await page.evaluate(() => { window.__hilSameLoad = true; });
}

const sameLoad = (page) => page.evaluate(() => window.__hilSameLoad === true).catch(() => false);
// 'old' (the page that loaded is still there), 'new' (another page, fully loaded) or 'between' (mid-navigation).
const which = (page) => page.evaluate(() => (window.__hilSameLoad === true ? 'old'
  : document.readyState === 'complete' ? 'new' : 'between')).catch(() => 'between');
const notice = (page) => page.evaluate(() => {
  const n = document.querySelector('#intellex-update .intellex-update-msg');
  return n ? n.textContent : '';
}).catch(() => '');

// A newer copy of `tool` lands; the host's token for it moves -> the new token.
async function land(tool) {
  const before = (await hostGet('/_api/status')).json.tools[tool];
  const r = await hil.hook('swap_tool', { tool });
  if (r.skip) test.skip(true, r.skip);
  const after = (await hostGet('/_api/status')).json.tools[tool];
  expect(after && after !== before, `/_api/status tools.${tool} after a copy landed: ${before} -> ${after}`).toBe(true);
  return after;
}

async function reloaded(page, within, what) {
  await expect.poll(() => which(page), { timeout: within, intervals: [250],
    message: `${what}: the page did not reload` }).toBe('new');
  await page.waitForTimeout(WATCH_MS + 500);     // and stays reloaded: one reload, not a loop
  expect(await which(page), `${what}: the page went on reloading`).toBe('new');
}

test('intellex.ui_tools_reload_when_safe a tool page reloads onto a newer copy once nothing would be lost',
  async ({ page, context, rec }) => {
    test.setTimeout(150_000);
    await hostGuard(context, rec);

    // ── The config tool ───────────────────────────────────────────────────────
    await settle(page, '/');
    await land('navicore');
    await reloaded(page, 3 * WATCH_MS + 2000, 'NC idle');
    expect(await notice(page), 'NC idle: no notice after the reload').toBe('');

    // An open window holds it, and says so.
    await settle(page, '/');
    await page.evaluate(() => openMeshStats());
    await expect(page.locator('#meshstats-modal.open'), 'NC Mesh stats opened').toBeVisible();
    await land('navicore');
    await expect.poll(() => notice(page), { timeout: 3 * WATCH_MS + 2000,
      message: 'NC an open window: no notice' }).toMatch(/A new version of the config tool has been downloaded.*once its open window is closed/);
    await page.waitForTimeout(2 * WATCH_MS);
    expect(await sameLoad(page), 'NC an open window: the page reloaded under it').toBe(true);
    await page.evaluate(() => closeMeshStats());
    await reloaded(page, 3 * WATCH_MS + 2000, 'NC the window closed');

    // A key just pressed holds it until TYPING_QUIET_MS has passed since.
    await settle(page, '/');
    await page.keyboard.press('Shift');
    const keyAt = Date.now();
    await land('navicore');
    await expect.poll(() => notice(page), { timeout: 3 * WATCH_MS + 2000,
      message: 'NC typing: no notice' }).toMatch(/once you stop typing/);
    await reloaded(page, 15_000 + 3 * WATCH_MS + 3000, 'NC 15 s after the last key');
    expect(Date.now() - keyAt, 'NC typing: reloaded before 15 s had passed since the key').toBeGreaterThanOrEqual(15_000);

    // ── The WCB Wizard ────────────────────────────────────────────────────────
    await settle(page, '/wcb/Wizard/');
    await land('wcb');
    await reloaded(page, 3 * WATCH_MS + 2000, 'WZ idle');
    expect(await notice(page), 'WZ idle: no notice after the reload').toBe('');

    await settle(page, '/wcb/Wizard/');
    await page.evaluate(() => { generalSettingsDirty = true; });    // eslint-disable-line no-undef
    await land('wcb');
    await expect.poll(() => notice(page), { timeout: 3 * WATCH_MS + 2000,
      message: 'WZ unpushed settings: no notice' }).toMatch(/A new version of the WCB Wizard has been downloaded.*once its changes are pushed/);
    await page.waitForTimeout(2 * WATCH_MS);
    expect(await sameLoad(page), 'WZ unpushed settings: the page reloaded under them').toBe(true);
    await page.evaluate(() => { generalSettingsDirty = false; });   // eslint-disable-line no-undef
    await reloaded(page, 3 * WATCH_MS + 2000, 'WZ the settings pushed');

    await page.waitForTimeout(1500);
    expect(rec.errors, 'page errors').toEqual([]);
    const bad = rec.bad.filter(b => !/^502 .*\/_api\/gh\//.test(b));   // the offline firmware listing (ui_tools_load)
    expect(bad, 'failed requests').toEqual([]);
  });
