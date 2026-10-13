// Needs no board: the page's start-up, the General section's derived state and the toasts (Wizard/app.js) - the
// low-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). Fake boards in the real page
// (lib/fake.js) where a board is needed: boardPull and boardGo run as on the bench, and only the wire is recorded.
// Runs standalone, in CI, and under the harness by id (s30_wizard.py, _FAKE_RESCAN_L).
//
// Nothing here is a real credential: the mesh passwords are dummies.
const { test, expect } = require('../lib/fixtures');
const { board, pullFake, edit } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince, shown } = require('../lib/rescan');

// The page loaded with no stored preferences and the splash left as the page shows it (lib/wizard.js openWizard, but
// without its click on the splash). The HTTP cache is off, as there, so the page runs the scripts on disk.
async function load(page, path = '/Wizard/index.html') {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  await page.goto(path);
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await ready(page);
}
const ready = (page) => page.waitForFunction(() => typeof boardManualConnect === 'function' && document.readyState !== 'loading');
const hasClass = (page, id, cls) => page.evaluate(({ id, cls }) => document.getElementById(id).classList.contains(cls), { id, cls });
const pushAllPending = (page) => page.evaluate(() => {
  const b = document.getElementById('btn-push-all');
  return [b.classList.contains('btn-pending'), b.classList.contains('btn-success')];
});
// The fixture's tokens without the ones whose verb is in `drop` (a board that never sent them).
const without = (tokens, ...drop) => tokens.filter((t) => !drop.includes(t.split(',')[0]));

test('wizard.init_fake_boot_steps the page starts with the splash open and the UI version in the footer; a start-up step that throws is logged and skipped, and every later one still runs - the board grid, the saved mode, the label combobox, the terminal\'s drag-to-resize, the splash, the release check and the footer', async ({ page }) => {
  // runWizardInit (app.js:142-160): each step inside safe(), the footer stamped last. Row init.boot_steps.
  const asked = [];
  await page.route('https://api.github.com/**', (route) => {
    asked.push(route.request().url());
    return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await load(page);
  const ver = await page.evaluate(() => UI_VERSION);
  expect(ver, 'DD.HH:MM.R.MON.YYYY').toMatch(/^\d\d\.\d\d:\d\d\.R\.[A-Z]{3}\.\d{4}$/);
  expect(await page.locator('#footer-ui-version').textContent()).toBe(ver);
  expect(await hasClass(page, 'splash-overlay', 'open')).toBe(true);
  expect(page.wizErrors).toEqual([]);

  // The first step throws (checkBrowserCompat is looked up by name when runWizardInit runs, on DOMContentLoaded).
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  await page.addInitScript(() => {
    document.addEventListener('DOMContentLoaded', () => {
      window.checkBrowserCompat = () => { throw new Error('hil: the compat check blew up'); };
    });
  });
  asked.length = 0;
  await page.reload();
  await ready(page);
  expect(consoleErrors.join('\n')).toContain('[init] checkBrowserCompat failed');
  expect(await page.evaluate(() => ({
    footer: document.getElementById('footer-ui-version').textContent === UI_VERSION,
    splash: document.getElementById('splash-overlay').classList.contains('open'),
    grid: !!document.getElementById('section-board-1'),                     // initSystemConfig
    controller: document.querySelector('#g-controller-seg .ctrl-none').classList.contains('selected'),
    mode: document.getElementById('btn-simple').classList.contains('active'), // loadModePreference
    combobox: !!document.getElementById('dev-combo-pop'),                    // initDeviceCombobox
  }))).toEqual({ footer: true, splash: true, grid: true, controller: true, mode: true, combobox: true });
  await expect.poll(() => asked.length, { message: 'fetchLatestFirmwareVersion ran' }).toBeGreaterThan(0);

  // initTerminalResize ran too: dragging the drawer's top edge up 100 px makes it 100 px taller.
  await page.locator('#splash-overlay button[onclick="splashGoConfig()"]').click();
  await page.locator('#btn-terminal').click();
  const h0 = await page.evaluate(() => document.getElementById('terminal-drawer').offsetHeight);
  const box = await page.locator('#terminal-resize-handle').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 - 100, { steps: 5 });
  await page.mouse.up();
  expect(await page.evaluate(() => document.getElementById('terminal-drawer').offsetHeight)).toBe(h0 + 100);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.init_fake_browser_warning a browser without Web Serial is told so by the warning banner; one with it never sees the banner', async ({ page }) => {
  // checkBrowserCompat (app.js:309-313). Row init.browser_warning.
  await load(page);
  expect(await hasClass(page, 'browser-warning', 'hidden'), 'Chrome with Web Serial').toBe(true);
  await page.addInitScript(() => { delete Navigator.prototype.serial; });
  await page.reload();
  await ready(page);
  expect(await page.evaluate(() => 'serial' in navigator)).toBe(false);
  expect(await hasClass(page, 'browser-warning', 'hidden'), 'no navigator.serial').toBe(false);
  expect(await page.locator('#browser-warning').isVisible()).toBe(true);
});

test('wizard.init_fake_theme_mode_prefs Advanced shows every advanced-only section (General\'s mesh channel, command characters and ETM, each board\'s WiFi, a board added later) and the ETM details only while ETM is enabled; Simple hides them; the mode and the light/dark theme are saved and come back on a reload', async ({ page }) => {
  // setMode, toggleTheme, loadThemePreference, loadModePreference (app.js:316-402), addBoardSection's mode pass
  // (667-670). Row init.theme_mode_prefs.
  await setup(page);
  const view = () => page.evaluate(() => ({
    simple: document.getElementById('btn-simple').classList.contains('active'),
    advanced: document.getElementById('btn-advanced').classList.contains('active'),
    hiddenAdvanced: [...document.querySelectorAll('.advanced-only')].filter((el) => el.classList.contains('hidden')).length,
    etmDetail: document.getElementById('etm-detail').classList.contains('visible'),
    saved: localStorage.getItem('wcb-mode'),
  }));
  let v = await view();
  expect(v).toMatchObject({ simple: true, advanced: false, etmDetail: false });
  expect(v.hiddenAdvanced).toBeGreaterThan(0);
  expect(await page.locator('#g-meshch').isVisible()).toBe(false);

  await page.locator('#btn-advanced').click();
  expect(await view()).toEqual({ simple: false, advanced: true, hiddenAdvanced: 0, etmDetail: true, saved: 'advanced' });
  for (const sel of ['#g-meshch', '#g-delimiter', '#g-funcchar', '#g-cmdchar', '#g-etm-timeout']) {
    expect.soft(await page.locator(sel).isVisible(), sel).toBe(true);
  }
  // ETM off: its details go, and Advanced picked again does not bring them back.
  await edit(page, '#g-etm-enabled', false);
  expect((await view()).etmDetail).toBe(false);
  await page.locator('#btn-advanced').click();
  expect((await view()).etmDetail).toBe(false);
  await edit(page, '#g-etm-enabled', true);
  expect((await view()).etmDetail).toBe(true);
  // A board section added in Advanced comes up with its advanced-only parts shown.
  await page.evaluate(() => addDiscoveredBoards([7]));
  expect(await page.evaluate(() => document.querySelectorAll('#section-board-7 .advanced-only.hidden').length)).toBe(0);

  // Reloaded: Advanced again, from storage.
  await page.reload();
  await ready(page);
  expect(await view()).toMatchObject({ simple: false, advanced: true, hiddenAdvanced: 0, saved: 'advanced' });
  await page.locator('#splash-overlay button[onclick="splashGoConfig()"]').click();
  await page.locator('#btn-simple').click();
  v = await view();
  expect(v).toMatchObject({ simple: true, advanced: false, etmDetail: false, saved: 'simple' });
  expect(v.hiddenAdvanced).toBeGreaterThan(0);

  // The theme: light, saved, and light again after a reload; then back to dark.
  const theme = () => page.evaluate(() => [document.documentElement.classList.contains('light'),
                                           document.getElementById('theme-toggle').textContent, localStorage.getItem('wcb-theme')]);
  expect(await theme()).toEqual([false, '🌙', null]);
  await page.locator('#theme-toggle').click();
  expect(await theme()).toEqual([true, '☀️', 'light']);
  await page.reload();
  await ready(page);
  expect(await theme()).toEqual([true, '☀️', 'light']);
  await page.locator('#splash-overlay button[onclick="splashGoConfig()"]').click();
  await page.locator('#theme-toggle').click();
  expect(await theme()).toEqual([false, '🌙', 'dark']);
  await page.reload();
  await ready(page);
  expect(await theme()).toEqual([false, '🌙', 'dark']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.init_fake_splash_choice the splash\'s Setup Wizard closes it and opens the guided setup on its welcome step; Config Tool closes it and opens nothing', async ({ page }) => {
  // showSplash, splashGoWizard, splashGoConfig (app.js:315-327). Row init.splash_choice.
  await load(page);
  await page.locator('#splash-overlay button[onclick="splashGoWizard()"]').click();
  expect(await page.evaluate(() => ({
    splash: document.getElementById('splash-overlay').classList.contains('open'),
    wizard: document.getElementById('wizard-modal').classList.contains('open'),
    open: _wizardOpen, step: wizardState.steps[wizardState.currentIdx],
    title: document.getElementById('wizard-modal-title').textContent,
  }))).toEqual({ splash: false, wizard: true, open: true, step: 'welcome', title: 'Setup Wizard' });
  await expect(page.locator('#wizard-body')).toContainText('Welcome to the WCB Setup Wizard');

  await load(page);
  expect(await hasClass(page, 'splash-overlay', 'open')).toBe(true);
  await page.locator('#splash-overlay button[onclick="splashGoConfig()"]').click();
  expect(await page.evaluate(() => [document.getElementById('splash-overlay').classList.contains('open'),
                                    document.getElementById('wizard-modal').classList.contains('open'), _wizardOpen]))
    .toEqual([false, false, false]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.init_fake_stale_branch_warning a leftover wcb_fw_branch override raises a warning that stays until dismissed (still there 15 s on), drawn above the splash with its Dismiss button in the window; a /dev/<branch>/Wizard preview with the same key set says nothing, since the preview\'s own branch is what it flashes', async ({ page }) => {
  // warnStaleFwBranchOverride (app.js:348-359), getFirmwareBranch (flasher.js:65-80). The toast itself is
  // wizard.app_fake_fw_check's. Under the splash (toasts were z-index 300, the splash 600) and with its Dismiss pushed
  // off the right edge by the unbreakable localStorage.removeItem('wcb_fw_branch'), the warning could not be dismissed
  // at all: fixed 2026-10-13 (styles.css .toast-container, .toast-msg). Row init.stale_branch_warning.
  await page.route('https://api.github.com/**', (route) => route.fulfill({ status: 404, body: '' }));
  // /dev/x/... is the repo's own files, as a preview deploy serves them.
  await page.route('**/dev/x/**', async (route) => {
    const u = new URL(route.request().url());
    const response = await route.fetch({ url: u.origin + u.pathname.replace('/dev/x', '') });
    return route.fulfill({ response });
  });
  const warnings = () => page.evaluate(() => [...document.querySelectorAll('#toast-container .toast-msg')]
    .map((e) => e.textContent).filter((t) => t.includes('Firmware source is overridden')));
  const loadWithKey = async (path) => {
    await load(page, path);
    await page.evaluate(() => localStorage.setItem('wcb_fw_branch', 'feature/stale'));
    await page.reload();
    await ready(page);
  };

  await loadWithKey('/dev/x/Wizard/index.html');
  expect(await page.evaluate(() => getFirmwareBranch()), 'the preview flashes its own branch').toBe('x');
  expect(await warnings()).toEqual([]);

  await loadWithKey('/Wizard/index.html');
  expect(await page.evaluate(() => getFirmwareBranch())).toBe('feature/stale');
  expect((await warnings()).length).toBe(1);
  expect((await warnings())[0]).toContain("overridden to branch 'feature/stale'");
  await page.waitForTimeout(15_000);
  expect((await warnings()).length, 'persistent: still up 15 s later').toBe(1);
  // It is raised while the splash is up, and drawn above it: its Dismiss button takes a real click there.
  expect(await hasClass(page, 'splash-overlay', 'open')).toBe(true);
  await page.locator('#toast-container .toast', { hasText: 'Firmware source is overridden' }).locator('.toast-dismiss')
    .click({ timeout: 5_000 });
  expect(await warnings()).toEqual([]);
  await page.evaluate(() => localStorage.removeItem('wcb_fw_branch'));
  expect(page.wizErrors).toEqual([]);
});

test('wizard.toast_fake_ui a toast shows its message as text (markup escaped), with Copy and Dismiss buttons; an info toast goes after its duration, an error stays at least 12 s whatever it asks for, a duration of 0 stays until dismissed, and a toast is drawn above an open modal', async ({ page }) => {
  // showToast (app.js:10410-10443), styles.css .toast-container (drawn above the modals since 2026-10-13). Row toast.ui.
  await page.clock.install();
  await setup(page);
  await page.evaluate(() => {
    window.__copied = [];
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (t) => { window.__copied.push(t); } } });
    document.getElementById('toast-container').innerHTML = '';
  });
  const present = (tag) => page.evaluate((tag) => [...document.querySelectorAll('#toast-container .toast')]
    .some((t) => t.querySelector('.toast-msg').textContent.includes(tag)), tag);
  await page.evaluate(() => {
    showToast('<b>hil-error</b>', 'error', 100);
    showToast('hil-info', 'info');
    showToast('hil-sticky', 'warning', 0);
  });
  const err = page.locator('#toast-container .toast.error');
  expect(await err.locator('.toast-msg').textContent()).toBe('<b>hil-error</b>');
  expect(await err.locator('.toast-msg b').count(), 'the markup is text, not an element').toBe(0);
  expect(await err.locator('.toast-copy').count()).toBe(1);
  expect(await err.locator('.toast-dismiss').count()).toBe(1);

  await page.clock.fastForward(3_600);
  expect([await present('hil-info'), await present('hil-error'), await present('hil-sticky')]).toEqual([false, true, true]);
  await page.clock.fastForward(7_600);                       // 11.2 s
  expect(await present('hil-error'), 'an error asked for 100 ms and is still up at 11 s').toBe(true);
  await page.clock.fastForward(1_000);                       // 12.2 s
  expect(await present('hil-error')).toBe(false);
  await page.clock.fastForward(60_000);
  expect(await present('hil-sticky')).toBe(true);

  // Copy puts the message on the clipboard and ticks; Dismiss removes the toast.
  const sticky = page.locator('#toast-container .toast', { hasText: 'hil-sticky' });
  await sticky.locator('.toast-copy').click();
  expect(await page.evaluate(() => window.__copied)).toEqual(['hil-sticky']);
  expect(await sticky.locator('.toast-copy').textContent()).toBe('✓');
  await sticky.locator('.toast-dismiss').click();
  expect(await present('hil-sticky')).toBe(false);

  // A toast raised while a modal is open (the setup wizard's own validation errors are toasts) is drawn above it,
  // so it can be read and its buttons used.
  await page.evaluate(() => { openWizard(); showToast('hil-over-modal', 'error'); });
  const over = page.locator('#toast-container .toast', { hasText: 'hil-over-modal' });
  await over.evaluate((t) => Promise.all(t.getAnimations().map((a) => a.finished)));   // its 200 ms slide-in
  expect(await over.evaluate((t) => {
    const r = t.querySelector('.toast-dismiss').getBoundingClientRect();
    return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)?.closest('.toast') === t;
  })).toBe(true);
  await over.locator('.toast-dismiss').click({ timeout: 5_000 });
  expect(await present('hil-over-modal')).toBe(false);
  expect(page.wizErrors).toEqual([]);
});
