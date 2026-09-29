// The chooser (Intellex src/launcher.html), with every route that would reach a port, a droid or GitHub answered here
// for the whole browser context (IX-WP4). /_api/ports, identify and discover come from the fixtures below; attach is
// fulfilled and recorded; the host behind the page is leashed as well. Branch changes go to the stage's settings.json
// only, and are put back to main.
//   rows: launcher.html:438-451; serial scan :771-866 (chip labels :793-801, identify only for 303A :805, the skips
//   :814, 'asking…' upgraded in place :830-855); WiFi rows :681-760; selection :453-489, auto-select once :419-423;
//   filter :491-511; theme :517-543; views :545-606 (wcb the default :563-573); Connect :647-657, openTools :628-645;
//   maintenance :343-394, :868-941, :973-1050; log :1156-1173; Back :1196-1207; 'Open the tool anyway' :1221.
const { test, expect, skipUnlessHost, hostGuard, hostPost } = require('../lib/fixtures');

skipUnlessHost(test);

const PORTS = [
  { path: 'COM91', description: 'USB Serial Device (COM91)', vid: 0x303A, pid: 0x1001, serial_number: 'HIL91' },
  { path: 'COM92', description: 'USB Serial Device (COM92)', vid: 0x303A, pid: 0x1001, serial_number: 'HIL92' },
  { path: 'COM93', description: 'USB Serial Device (COM93)', vid: 0x303A, pid: 0x1001, serial_number: 'HIL93' },
  { path: 'COM94', description: 'USB-Enhanced-SERIAL CH9102 (COM94)', vid: 0x1A86, pid: 0x55D4, serial_number: '' },
  { path: 'COM95', description: 'Silicon Labs CP210x USB to UART Bridge (COM95)', vid: 0x10C4, pid: 0xEA60,
    serial_number: '' },
  { path: 'COM96', description: 'Standard Serial over Bluetooth link (COM96)', vid: null, pid: null, serial_number: '' },
  { path: '/dev/tty.usbmodem9', description: 'USB JTAG/serial debug unit', vid: 0x303A, pid: 0x1001, serial_number: '' },
];
const IDENTIFY = { COM91: { ok: true, version: 'v9.9.9_HILNC', busy: false },
                   COM92: { ok: false, version: null, busy: true },
                   COM93: { ok: false, version: null, busy: false } };
const cand = (o) => Object.assign({ via: '192.168.4.2', routable: true, reachable: true, kind: 'unknown',
  isNaviCore: false, isRelay: false, isWcb: false, isMesh: false, version: null, relayId: null, alias: null, peers: [],
  hint: '' }, o);
const DISCOVER = { candidates: [
  cand({ host: '192.168.4.1', kind: 'navicore', isNaviCore: true, version: 'v9.9.9_HILNC' }),
  cand({ host: '192.168.4.19', kind: 'relay', isRelay: true, isMesh: true, relayId: '19', alias: 'Mgmt Relay',
         peers: [{ alias: 'Body', fw: '6.2.1' }] }),
  cand({ host: '192.168.4.21', kind: 'wcb', isWcb: true, isMesh: true, relayId: '1', alias: 'Body WCB' }),
  cand({ host: '192.168.4.30', reachable: false, hint: 'routed via 192.168.4.2 but no TCP' }),
] };

async function open(page, context, rec) {
  await context.route(/\/_api\/ports(\?|$)/, r => r.fulfill({ json: { ports: PORTS } }));
  const calls = await hostGuard(context, rec, {
    identify: (route, req) => route.fulfill({ json: IDENTIFY[req.postDataJSON().port] ||
                                              { ok: false, version: null, busy: false } }),
    discover: route => route.fulfill({ json: DISCOVER }),
    attach: (route, req) => route.fulfill({ json: { ok: true, target: 'fulfilled by the spec' } }),
  });
  await page.goto('/_launcher', { waitUntil: 'load' });
  return calls;
}

const text = (page, sel) => page.locator(sel).first().textContent();
const rows = (page, list) => page.evaluate((id) => [...document.querySelectorAll(`#${id} .row`)].map(r => ({
  title: r.querySelector('b').textContent, sub: r.querySelector('small').textContent,
  up: r.querySelector('.dot').classList.contains('up'), sel: r.classList.contains('sel') })), list);

test('intellex.ui_launcher the chooser with fixture ports and droids', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);            // an action on a hidden control fails here, not at the 300 s test timeout
  // The app asks for a 1280x880 window (Intellex src/app.py, winsize.fit). At Playwright's default 720 px the folded
  // maintenance bar covers the bottom rows of a list this long.
  await page.setViewportSize({ width: 1280, height: 1000 });
  const calls = await open(page, context, rec);

  // ── the serial list: what the descriptor proves, the skips, identify for native USB only ──────────────────
  await expect.poll(async () => (await rows(page, 'serial')).map(r => r.sub), { timeout: 10_000,
    message: 'the serial rows after identify' }).toEqual(['NaviCore v9.9.9_HILNC',
    'Espressif native USB — in use by another program', 'Espressif native USB — did not answer as a NaviCore',
    'CH9102 bridge', 'CP210x bridge']);
  const serial = await rows(page, 'serial');
  expect(serial.map(r => r.title), 'serial rows (the Bluetooth and /dev/tty.* ports skipped)')
    .toEqual(['COM91', 'COM92', 'COM93', 'COM94', 'COM95']);
  expect(serial.map(r => r.up), 'only the port that answered as a NaviCore is lit').toEqual([true, false, false, false,
                                                                                            false]);
  expect(calls.filter(c => c.name === 'identify').map(c => c.body.port).sort(),
    'identify asked of the Espressif native-USB ports only').toEqual(['COM91', 'COM92', 'COM93']);

  // ── the WiFi list: what each droid is, and what is behind a doorway ────────────────────────────────────────
  await expect.poll(async () => (await rows(page, 'wifi')).length, { timeout: 10_000 }).toBe(3);
  const wifi = await rows(page, 'wifi');
  expect(wifi.map(r => [r.title, r.sub]), 'the WiFi rows').toEqual([
    ['NaviCore at 192.168.4.1', 'v9.9.9_HILNC · direct · via 192.168.4.2'],
    ['Mgmt Relay at 192.168.4.19', 'bridges to the mesh · WCB #19 · sees Body 6.2.1'],
    ['Body WCB at 192.168.4.21', 'a WCB, and the way to the mesh · #1 · no boards seen yet']]);
  await expect.poll(() => text(page, '#scanstate'), { timeout: 10_000,
    message: 'a routable host that does not answer yet keeps the chooser watching' }).toBe('watching…');

  // ── filter tabs and counts ──────────────────────────────────────────────────────────────────────────────────
  expect([await text(page, '#c-all'), await text(page, '#c-wifi'), await text(page, '#c-usb')]).toEqual(['8', '3', '5']);
  await page.click('#tabs button[data-filter="usb"]');
  expect(await page.evaluate(() => [document.getElementById('wifi').classList.contains('hidden'),
    document.getElementById('serial').classList.contains('hidden')]), 'the USB tab hides WiFi only').toEqual([true, false]);
  await page.click('#tabs button[data-filter="all"]');

  // ── selection: auto-selected once, in document order; a user's choice is never moved ──────────────────────
  expect(await text(page, '#selTitle'), 'auto-select picks the first row (WiFi renders above USB)')
    .toBe('NaviCore at 192.168.4.1');
  expect(await text(page, '#actionNote'), 'the default layout is WCB').toBe('Opens WCB on 192.168.4.1.');
  await page.locator('#serial .row', { hasText: 'COM95' }).click();
  const discovers = calls.filter(c => c.name === 'discover').length;
  await expect.poll(() => calls.filter(c => c.name === 'discover').length, { timeout: 10_000,
    message: 'the watch did not rescan the WiFi list' }).toBeGreaterThan(discovers);
  await page.waitForTimeout(500);
  expect(await text(page, '#selTitle'), 'a rescan moved the user\'s selection').toBe('COM95');
  await page.locator('#wifi .row', { hasText: 'Mgmt Relay' }).click();
  const n2 = calls.filter(c => c.name === 'discover').length;
  await expect.poll(() => calls.filter(c => c.name === 'discover').length, { timeout: 10_000 }).toBeGreaterThan(n2);
  await page.waitForTimeout(500);
  expect(await text(page, '#selTitle'), 'a redrawn WiFi row did not keep its selection')
    .toBe('Mgmt Relay at 192.168.4.19');

  // ── theme and layout, remembered ────────────────────────────────────────────────────────────────────────────
  const theme0 = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  await page.click('#theme');
  const theme1 = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  expect(theme1, 'the theme button did not switch the theme').not.toBe(theme0);
  expect(await page.evaluate(() => localStorage.getItem('intellex_theme'))).toBe(theme1);
  expect(await page.evaluate(() => document.querySelector('.pick[aria-pressed="true"]').dataset.view),
    'the default layout').toBe('wcb');
  await page.click('.pick[data-view="split"]');
  expect(await page.evaluate(() => localStorage.getItem('intellex_view'))).toBe('split');
  expect(await text(page, '#actionNote')).toBe('Opens Side by side on 192.168.4.19.');

  // ── maintenance: what needs attention shows without opening the panel ──────────────────────────────────────
  await expect.poll(() => text(page, '#m-fw .t'), { timeout: 10_000 }).toBe('NaviCore + WCB firmware not cached');
  expect(await page.evaluate(() => document.querySelector('#m-fw .dot').classList.contains('warn'))).toBe(true);
  await expect.poll(() => text(page, '#m-more .t'), { timeout: 10_000 }).toBe('docs not downloaded');
  expect(await text(page, '#attntext')).toBe('2 things need attention');
  expect(await page.evaluate(() => document.getElementById('opendocs').disabled), 'Docs with no docs stored').toBe(true);
  expect(await text(page, '#br-note'), 'offline, the branch list says why it could not refresh').toContain('Branch list:');

  // ── the branch picker: Other… with a typed name, then Set (inside the folded maintenance panel) ────────────
  await page.evaluate(() => { document.getElementById('maint').open = true; });
  try {
    const opts = await page.evaluate(() => [...document.querySelectorAll('#br-wcb option')].map(o => o.value));
    expect(opts, 'the WCB branch options (offline: the current branch, main, Other…)').toEqual(['main', ':other:']);
    await page.selectOption('#br-wcb', ':other:');
    expect(await page.isVisible('#br-wcb-txt'), 'Other… shows the text box').toBe(true);
    await page.fill('#br-wcb-txt', 'feature/hil-launch');
    await page.click('#br-save');
    await expect.poll(() => text(page, '#status'), { timeout: 10_000 }).toContain('branch set');
    const branches = await (await page.request.get('/_api/branches')).json();
    expect(branches.wcb, 'the branch Set wrote').toBe('feature/hil-launch');
    await page.reload({ waitUntil: 'load' });
    await expect.poll(() => text(page, '#m-more .t'), { timeout: 10_000,
      message: 'a non-main branch is not called out in the summary' }).toBe('WCB → feature/hil-launch');
    expect(await page.evaluate(() => document.getElementById('maint').open),
      'the folded panel does not open by itself').toBe(false);
    expect(await text(page, '#attntext')).toBe('3 things need attention');
  } finally {
    await hostPost('/_api/branch', { product: 'wcb', branch: 'main' });
    await hostPost('/_api/branch', { product: 'navicore', branch: 'main' });
  }

  // ── the log ──────────────────────────────────────────────────────────────────────────────────────────────────
  await page.click('#showlog');
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.getElementById('logtail')).display))
    .toBe('block');
  expect((await text(page, '#logtail')).length, 'the log tail is empty').toBeGreaterThan(0);
  expect(await text(page, '#showlog')).toBe('Hide log');
  await page.click('#showlog');
  expect(await page.evaluate(() => getComputedStyle(document.getElementById('logtail')).display)).toBe('none');

  // ── Back, when the host is attached ─────────────────────────────────────────────────────────────────────────
  await context.route(/\/_api\/status(\?|$)/, r => r.fulfill({ json: { attached: true, target: 'serial COM91',
    lastError: '', reconnecting: false, wantsLink: true, kind: 'serial', role: '', relayId: null } }));
  await page.reload({ waitUntil: 'load' });
  await expect.poll(() => page.isVisible('#back'), { timeout: 10_000, message: 'no Back button while attached' })
    .toBe(true);
  expect(await text(page, '#status')).toBe('currently on serial COM91 — pick another, or go back');
  await context.unroute(/\/_api\/status(\?|$)/);

  // ── F5 reloads (the app window has no browser chrome) ───────────────────────────────────────────────────────
  const t0 = await page.evaluate(() => performance.timeOrigin);
  await Promise.all([page.waitForEvent('load'), page.keyboard.press('F5')]);
  expect(await page.evaluate(() => performance.timeOrigin), 'F5 did not reload the page').not.toBe(t0);

  // ── Connect: the attach body carries the relay's id, then the layout opens ──────────────────────────────────
  await expect.poll(async () => (await rows(page, 'wifi')).length, { timeout: 10_000 }).toBe(3);
  await page.click('.pick[data-view="wcb"]');
  await page.locator('#wifi .row', { hasText: 'Mgmt Relay' }).click();
  await Promise.all([page.waitForURL(/\/_shell\?view=wcb$/, { timeout: 15_000 }), page.click('#go')]);
  const attach = calls.filter(c => c.name === 'attach').pop();
  expect(attach && attach.body, 'the attach body').toEqual({ kind: 'ws', host: '192.168.4.19', role: 'relay',
                                                            relayId: 19 });

  // ── Separate windows: the config tool here, the Wizard in a popup (no window backend under host.py) ─────────
  await page.goto('/_launcher', { waitUntil: 'load' });
  await expect.poll(async () => (await rows(page, 'wifi')).length, { timeout: 10_000 }).toBe(3);
  await page.click('.pick[data-view="sep"]');
  await page.locator('#wifi .row', { hasText: 'NaviCore at' }).click();
  const [popup] = await Promise.all([context.waitForEvent('page', { timeout: 15_000 }), page.click('#go')]);
  await expect.poll(() => popup.url(), { timeout: 10_000 }).toMatch(/\/wcb\/Wizard\/$/);
  await page.waitForURL(/\/$/, { timeout: 15_000 });
  expect(calls.filter(c => c.name === 'attach').pop().body, 'the NaviCore attach body')
    .toEqual({ kind: 'ws', host: '192.168.4.1', role: 'navicore' });
  await popup.close();

  // ── 'Open the tool anyway' needs no droid ────────────────────────────────────────────────────────────────────
  await page.goto('/_launcher', { waitUntil: 'load' });
  await page.click('.pick[data-view="wcb"]');
  await Promise.all([page.waitForURL(/\/_shell\?view=wcb$/, { timeout: 15_000 }), page.click('#offline')]);
  expect(rec.errors, 'page errors').toEqual([]);
});
