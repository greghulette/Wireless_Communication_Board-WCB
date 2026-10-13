// The two-tool window (Intellex src/shell.html, IX-WP4). Every route that reaches past the host is answered for the
// whole context (hostGuard), which covers both tool frames and the launcher inside the chooser overlay; discover
// answers "nothing found", so the overlay's scan stays inert.
//   views and ?view= :130-217; frames never destroyed :142-163; both panes preloaded after PRELOAD_DELAY_MS (4 s)
//   :165-188; the split's zoom-to-fit (DESIGN_W 1180, MIN_ZOOM 0.55) :219-284; the divider drag :300-323; New window
//   moves the tool out :325-375; the label :377-396; the chooser overlay and its postMessages :397-444.
const { test, expect, skipUnlessHost, hostGuard } = require('../lib/fixtures');

skipUnlessHost(test);

const DESIGN_W = 1180, MIN_ZOOM = 0.55;

// A frame's path, or '' while it has no URL yet (a frame just created is about:blank or empty for a moment).
const pathOf = (f) => { try { return new URL(f.url()).pathname; } catch (_) { return ''; } };

test('intellex.ui_shell the two-tool window', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  // 1406 px: each split pane is (1406 - 6) / 2 = 700 px, the plan's worked example (zoom 700/1180 = 0.593).
  await page.setViewportSize({ width: 1406, height: 900 });
  await hostGuard(context, rec, {
    discover: route => route.fulfill({ json: { candidates: [] } }),
    identify: route => route.fulfill({ json: { ok: false, version: null, busy: false } }),
  });
  await page.goto('/_shell?view=wcb', { waitUntil: 'load' });
  const pressed = () => page.evaluate(() => ['t-nc', 't-wcb', 't-split'].map(id =>
    document.getElementById(id).getAttribute('aria-pressed')));
  const frames = () => page.evaluate(() => ({
    nc: !!document.querySelector('#p-nc iframe'), wcb: !!document.querySelector('#p-wcb iframe') }));

  expect(await pressed(), 'the tab states for ?view=wcb').toEqual(['false', 'true', 'false']);
  expect(await frames(), 'the visible pane first, the other not yet').toEqual({ nc: false, wcb: true });
  await expect.poll(frames, { timeout: 8_000, message: 'the hidden pane was not preloaded (4 s)' })
    .toEqual({ nc: true, wcb: true });

  await page.click('#t-nc');
  expect(await pressed()).toEqual(['true', 'false', 'false']);
  expect(new URL(page.url()).searchParams.get('view'), 'the URL follows the view').toBe('nc');
  expect(await page.evaluate(() => localStorage.getItem('intellex_view'))).toBe('nc');

  // ── the split: each document gets DESIGN_W px, zoomed to fit its pane ──────────────────────────────────────
  await page.click('#t-split');
  expect(await pressed()).toEqual(['false', 'false', 'true']);
  const fit = () => page.evaluate(() => ['p-nc', 'p-wcb'].map(id => {
    const p = document.getElementById(id), f = p.querySelector('iframe');
    return { w: p.clientWidth, zoom: f.style.zoom, width: f.style.width };
  }));
  await expect.poll(async () => (await fit())[0].zoom !== '', { timeout: 5_000 }).toBe(true);
  for (const pane of await fit()) {
    const z = Math.max(MIN_ZOOM, Math.min(1, pane.w / DESIGN_W));
    expect(Math.abs(parseFloat(pane.zoom) - z), `pane ${pane.w} px: zoom ${pane.zoom}`).toBeLessThan(1e-6);
    expect(pane.width, `pane ${pane.w} px: frame width`).toBe(Math.round(pane.w / z) + 'px');
  }
  const [ncPane] = await fit();
  expect(ncPane.w, 'a 1406 px window splits into 700 px panes').toBe(700);
  expect(parseFloat(ncPane.zoom).toFixed(3)).toBe('0.593');

  // ── drag the divider to 35 %: remembered ────────────────────────────────────────────────────────────────────
  const box = await page.locator('#split').boundingBox();
  const panes = await page.locator('#panes').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(panes.x + panes.width * 0.35, box.y + box.height / 2, { steps: 8 });
  await page.mouse.up();
  const split = parseFloat(await page.evaluate(() => localStorage.getItem('intellex_split')));
  expect(Math.abs(split - 35), `intellex_split after the drag: ${split}`).toBeLessThan(2);

  // ── the chooser overlay: over the tools, never reloading them ───────────────────────────────────────────────
  const origin = async () => {
    const out = {};
    for (const f of page.frames()) {
      const p = pathOf(f);
      if (p === '/' || p === '/wcb/Wizard/') out[p] = await f.evaluate(() => performance.timeOrigin);
    }
    return out;
  };
  const before = await origin();
  expect(Object.keys(before).sort(), 'both tool frames are up').toEqual(['/', '/wcb/Wizard/']);
  await page.click('#target');
  await expect.poll(() => page.evaluate(() => !!document.querySelector('#chooser iframe[src="/_launcher"]')),
    { timeout: 5_000, message: '#target did not open the chooser overlay' }).toBe(true);
  await page.click('#chooser button');                                        // its Close button
  await expect.poll(() => page.evaluate(() => !document.getElementById('chooser')), { timeout: 5_000 }).toBe(true);
  const wiz = page.frames().find(f => pathOf(f) === '/wcb/Wizard/');
  await wiz.evaluate(() => window.parent.postMessage({ intellex: 'open-chooser' }, location.origin));
  await expect.poll(() => page.evaluate(() => !!document.getElementById('chooser')), { timeout: 5_000,
    message: 'a tool frame\'s open-chooser message did not open the overlay' }).toBe(true);
  let launcher = null;
  await expect.poll(() => { launcher = page.frames().find(f => pathOf(f) === '/_launcher'); return !!launcher; },
    { timeout: 5_000 }).toBe(true);
  await launcher.waitForLoadState('load');
  expect(await launcher.evaluate(() => getComputedStyle(document.getElementById('openwithsect')).display),
    'the layout picker inside the shell').toBe('none');
  await launcher.evaluate(() => window.parent.postMessage({ intellex: 'close-chooser' }, location.origin));
  await expect.poll(() => page.evaluate(() => !document.getElementById('chooser')), { timeout: 5_000,
    message: 'the launcher\'s close-chooser message did not close the overlay' }).toBe(true);
  expect(await origin(), 'opening and closing the chooser reloaded a tool').toEqual(before);
  await expect.poll(() => page.evaluate(() => document.getElementById('target').textContent), { timeout: 5_000 })
    .toBe('not attached ▾');

  // ── New window moves the other tool out (no window backend under host.py, so a popup) ───────────────────────
  await page.click('#t-nc');
  const [popup] = await Promise.all([context.waitForEvent('page', { timeout: 10_000 }), page.click('#t-win')]);
  await expect.poll(() => popup.url(), { timeout: 10_000 }).toMatch(/\/wcb\/Wizard\/$/);
  expect(await frames(), 'the Wizard pane was not handed to the new window').toEqual({ nc: true, wcb: false });
  expect(await pressed(), 'the view after moving the Wizard out').toEqual(['true', 'false', 'false']);
  await popup.close();
  expect(rec.errors, 'page errors').toEqual([]);
});

// ── The 2026-10-12 re-scan's low-risk shell rows ─────────────────────────────────────────────────────────────────────
const framesOf = (page) => page.evaluate(() => ({
  nc: !!document.querySelector('#p-nc iframe'), wcb: !!document.querySelector('#p-wcb iframe') }));
const pressedOf = (page) => page.evaluate(() => ['t-nc', 't-wcb', 't-split'].map((id) =>
  document.getElementById(id).getAttribute('aria-pressed')));

// IX re-scan shell.new_window (shell.html:348-375 $('t-win').onclick, :152-188 ensure / preloadOthers): a tool moved
// out to its own window before the preload is not recreated by it, and an explicit tab click brings its frame back.
test('intellex.ui_shell_moved_out a tool moved to its own window stays out until its tab is clicked', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1406, height: 900 });
  await hostGuard(context, rec, {
    discover: (route) => route.fulfill({ json: { candidates: [] } }),
    identify: (route) => route.fulfill({ json: { ok: false, version: null, busy: false } }),
  });
  await page.goto('/_shell?view=nc', { waitUntil: 'load' });
  expect(await framesOf(page), 'before the preload').toEqual({ nc: true, wcb: false });
  const [popup] = await Promise.all([context.waitForEvent('page', { timeout: 10_000 }), page.click('#t-win')]);
  await expect.poll(() => popup.url(), { timeout: 10_000 }).toMatch(/\/wcb\/Wizard\/$/);
  await page.waitForTimeout(6_000);                      // past PRELOAD_DELAY_MS (4 s)
  expect(await framesOf(page), 'the preload recreated the Wizard handed to its own window').toEqual({ nc: true, wcb: false });
  expect(await pressedOf(page)).toEqual(['true', 'false', 'false']);
  await popup.close();
  await page.click('#t-wcb');
  await expect.poll(() => framesOf(page), { timeout: 5_000, message: 'the WCB tab did not bring the Wizard back' })
    .toEqual({ nc: true, wcb: true });
  expect(await pressedOf(page)).toEqual(['false', 'true', 'false']);
  await expect.poll(() => page.frames().some((f) => pathOf(f) === '/wcb/Wizard/'), { timeout: 10_000 }).toBe(true);
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan shell.link_label (shell.html:377-396 labelFor / refresh, every 2 s): what the bar says for each link the
// host can report. /_api/status is fulfilled for the shell's own document only; the tool frames get 'nothing
// attached', so neither tool tries to connect.
test('intellex.ui_shell_link_labels the bar names the link: WiFi, relay, WCB doorway, USB, reconnecting, not attached', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await hostGuard(context, rec, {
    discover: (route) => route.fulfill({ json: { candidates: [] } }),
    identify: (route) => route.fulfill({ json: { ok: false, version: null, busy: false } }),
  });
  const base = { lastError: '', reconnecting: false, kind: '', role: '', relayId: null };
  let shellStatus = { attached: false, target: '', wantsLink: false };
  let fail = false;
  await context.route(/\/_api\/status(\?|$)/, (route, req) => {
    if (req.frame() !== page.mainFrame()) {
      return route.fulfill({ json: Object.assign({}, base, { attached: false, target: '', wantsLink: false }) });
    }
    if (fail) return route.abort('failed');
    return route.fulfill({ json: Object.assign({}, base, shellStatus) });
  });
  await page.goto('/_shell?view=nc', { waitUntil: 'load' });
  const label = () => page.evaluate(() => document.getElementById('target').textContent);
  for (const [st, want] of [
    [{ attached: true, target: 'ws://192.168.4.1/ws', wantsLink: true }, 'WiFi 192.168.4.1 ▾'],
    [{ attached: true, target: 'relay 192.168.4.19 → mesh', wantsLink: true }, 'Relay 192.168.4.19 → mesh ▾'],
    [{ attached: true, target: 'WCB 192.168.4.1 → mesh', wantsLink: true }, 'WCB 192.168.4.1 → mesh ▾'],
    [{ attached: true, target: 'serial COM7', wantsLink: true }, 'USB COM7 ▾'],
    [{ attached: false, target: '', wantsLink: true }, 'reconnecting… ▾'],
    [{ attached: false, target: '', wantsLink: false }, 'not attached ▾'],
    [{ attached: true, target: 'ws://192.168.4.1/ws', wantsLink: true }, 'WiFi 192.168.4.1 ▾'],
  ]) {
    shellStatus = st;
    await expect.poll(label, { timeout: 6_000, message: `the label for ${JSON.stringify(st)}` }).toBe(want);
  }
  fail = true;                                           // a status request that fails reads as not attached
  await expect.poll(label, { timeout: 6_000 }).toBe('not attached ▾');
  fail = false;
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan shell.chooser_overlay (shell.html:439-444): the shell acts on open-chooser and close-chooser from its own
// origin only. A message dispatched with another origin, and one posted by a frame of an opaque origin (a data: URL),
// are ignored; the same message from this origin works.
test('intellex.ui_shell_foreign_message a chooser message from another origin is ignored', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await hostGuard(context, rec, {
    discover: (route) => route.fulfill({ json: { candidates: [] } }),
    identify: (route) => route.fulfill({ json: { ok: false, version: null, busy: false } }),
  });
  await page.goto('/_shell?view=nc', { waitUntil: 'load' });
  const open = () => page.evaluate(() => !!document.getElementById('chooser'));
  const send = (what, origin) => page.evaluate(([w, o]) => window.dispatchEvent(new MessageEvent('message',
    { data: { intellex: w }, origin: o === 'self' ? location.origin : o })), [what, origin]);
  await send('open-chooser', 'http://evil.example');
  await page.waitForTimeout(1_000);
  expect(await open(), 'open-chooser from http://evil.example opened the chooser').toBe(false);
  await page.evaluate(() => {
    const f = document.createElement('iframe');
    f.id = 'hil-foreign';
    f.src = 'data:text/html,<script>parent.postMessage({intellex:"open-chooser"},"*")<\/script>';
    document.body.appendChild(f);
  });
  await page.waitForTimeout(1_500);
  expect(await open(), 'open-chooser posted by an opaque-origin frame opened the chooser').toBe(false);
  await send('open-chooser', 'self');
  await expect.poll(open, { timeout: 5_000, message: 'the same message from this origin did nothing' }).toBe(true);
  await send('close-chooser', 'http://evil.example');
  await page.waitForTimeout(1_000);
  expect(await open(), 'close-chooser from another origin closed the chooser').toBe(true);
  await send('close-chooser', 'self');
  await expect.poll(open, { timeout: 5_000 }).toBe(false);
  expect(rec.errors, 'page errors').toEqual([]);
});
