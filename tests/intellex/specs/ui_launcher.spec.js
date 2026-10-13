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

// IX re-scan launcher.connect (launcher.html:647-657 connect): an attach the host refuses leaves the chooser where it
// is, with the host's words in #status in the error style, and opens nothing.
test('intellex.ui_launcher_connect_error a refused attach stays on the chooser and says why', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1280, height: 1000 });
  let reply = { status: 502, json: { ok: false, error: 'HIL refused: COM91 is busy' } };
  await context.route(/\/_api\/ports(\?|$)/, r => r.fulfill({ json: { ports: PORTS } }));
  const calls = await hostGuard(context, rec, {
    identify: (route, req) => route.fulfill({ json: IDENTIFY[req.postDataJSON().port] ||
                                              { ok: false, version: null, busy: false } }),
    discover: route => route.fulfill({ json: DISCOVER }),
    attach: route => route.fulfill({ status: reply.status, json: reply.json }),
    'open-window': route => route.fulfill({ status: 501, json: { ok: false, error: 'no window backend' } }),
  });
  const navigations = [];
  page.on('framenavigated', f => { if (f === page.mainFrame()) navigations.push(f.url()); });
  const popups = [];
  context.on('page', p => popups.push(p.url()));
  await page.goto('/_launcher', { waitUntil: 'load' });
  await expect.poll(async () => (await rows(page, 'serial')).length, { timeout: 10_000 }).toBe(5);
  const status = () => page.evaluate(() => {
    const s = document.getElementById('status');
    return { text: s.textContent, cls: s.className };
  });

  for (const view of ['wcb', 'sep']) {
    await page.click(`.pick[data-view="${view}"]`);
    await page.locator('#serial .row', { hasText: 'COM91' }).click();
    const n = calls.filter(c => c.name === 'attach').length;
    const nav0 = navigations.length;
    await page.click('#go');
    await expect.poll(() => calls.filter(c => c.name === 'attach').length, { timeout: 5_000 }).toBe(n + 1);
    expect(calls.filter(c => c.name === 'attach').pop().body, 'the attach body').toEqual({ kind: 'serial',
                                                                                         port: 'COM91' });
    await expect.poll(status, { timeout: 5_000, message: `layout ${view}: the refusal is not shown` })
      .toEqual({ text: 'HIL refused: COM91 is busy', cls: 'status err' });
    await page.waitForTimeout(2_000);
    expect(new URL(page.url()).pathname, `layout ${view}: the page left the chooser`).toBe('/_launcher');
    expect(navigations.slice(nav0), `layout ${view}: navigated after a refused attach`).toEqual([]);
    expect(calls.filter(c => c.name === 'open-window').length, `layout ${view}: asked for a second window`).toBe(0);
    expect(popups, `layout ${view}: opened a window`).toEqual([]);
  }

  // A refusal with no words: the chooser's own.
  reply = { status: 502, json: { ok: false } };
  await page.click('#go');
  await expect.poll(status, { timeout: 5_000 }).toEqual({ text: 'could not connect', cls: 'status err' });
  expect(new URL(page.url()).pathname).toBe('/_launcher');

  // And then it works: the same button goes on to the tools (a control that the refusals above left it usable).
  reply = { status: 200, json: { ok: true, target: 'serial COM91', role: 'navicore' } };
  await page.click('.pick[data-view="wcb"]');
  await Promise.all([page.waitForURL(/\/_shell\?view=wcb$/, { timeout: 15_000 }), page.click('#go')]);
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan launcher.maintenance_summary (launcher.html:343-394 paintMaint, :868-901 checkUi, :1176-1191 Update tools):
// a stale bundled tool shows in the folded summary and in the attention count, and the summary's Update drives the
// panel's own Update tools button.
test('intellex.ui_launcher_tools_stale a stale tool is called out in the summary and its Update works', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1280, height: 1000 });
  const ver = (stale, wstale) => ({
    bundled: 'HILNC-a', published: stale ? 'HILNC-b' : 'HILNC-a', stale, checked: true, reason: '',
    wcb: { bundled: 'HILWIZ-a', published: wstale ? 'HILWIZ-b' : 'HILWIZ-a', stale: wstale, checked: true, reason: '' },
  });
  let current = ver(true, true);
  await context.route(/\/_api\/webui-version(\?|$)/, r => r.fulfill({ json: current }));
  await context.route(/\/_api\/ports(\?|$)/, r => r.fulfill({ json: { ports: [] } }));
  await hostGuard(context, rec, { discover: route => route.fulfill({ json: { candidates: [] } }) });
  const updates = [];
  await context.route(/\/_api\/update-webui(\?.*)?$/, (route, req) => {
    updates.push(new URL(req.url()).search);
    rec.fulfilled.add(req.url());
    current = ver(false, false);           // what the host reports once the update has landed
    return route.fulfill({ json: { ok: true, version: 'HILNC-b', wcbVersion: 'HILWIZ-b', log: 'HIL stub' } });
  });
  await page.goto('/_launcher', { waitUntil: 'load' });
  const mline = (id) => page.evaluate((i) => {
    const el = document.getElementById(i);
    const a = el.querySelector('a');
    return { text: el.querySelector('.t').textContent, warn: el.querySelector('.dot').classList.contains('warn'),
             action: a.style.display === 'none' ? null : a.textContent };
  }, id);
  await expect.poll(() => mline('m-tools'), { timeout: 10_000, message: 'the stale-tool summary line' })
    .toEqual({ text: 'config tool + Wizard update available', warn: true, action: 'Update' });
  await expect.poll(() => text(page, '#attntext'), { timeout: 10_000 }).toBe('3 things need attention');
  const uiver = await page.evaluate(() => document.getElementById('uiver').textContent);
  expect(uiver, 'the panel\'s tool line').toBe('config tool HILNC-a — update available (HILNC-b)WCB Wizard HILWIZ-a — '
                                               + 'update available (HILWIZ-b)');
  expect(await page.evaluate(() => document.getElementById('maint').open), 'the summary opened the folded panel')
    .toBe(false);
  expect(await page.evaluate(() => document.getElementById('maint-sum').textContent), 'the folded panel\'s own line')
    .toContain('config tool + Wizard update available');

  // One stale tool is named alone.
  current = ver(false, true);
  await page.click('#rescan');
  await expect.poll(() => mline('m-tools'), { timeout: 10_000 })
    .toEqual({ text: 'Wizard update available', warn: true, action: 'Update' });
  current = ver(true, false);
  await page.click('#rescan');
  await expect.poll(() => mline('m-tools'), { timeout: 10_000 })
    .toEqual({ text: 'config tool update available', warn: true, action: 'Update' });

  // The summary's Update is the panel's Update tools: both tools (no ?tool=), then the line clears.
  await page.click('#m-tools-a');
  await expect.poll(() => updates.length, { timeout: 5_000, message: 'Update did not POST /_api/update-webui' }).toBe(1);
  expect(updates[0], 'Update tools asks for both tools').toBe('');
  expect(await page.evaluate(() => document.getElementById('maint').open), 'Update opened the panel').toBe(true);
  await expect.poll(() => text(page, '#status'), { timeout: 10_000 }).toBe('updated — config tool HILNC-b, Wizard HILWIZ-b');
  // The summary's action stays offered when the tools are current (paintMaint passes 'Update' either way).
  await expect.poll(() => mline('m-tools'), { timeout: 10_000 })
    .toEqual({ text: 'Tools up to date', warn: false, action: 'Update' });
  await expect.poll(() => text(page, '#attntext'), { timeout: 10_000 }).toBe('2 things need attention');
  expect(rec.errors, 'page errors').toEqual([]);
});

// ── The 2026-10-12 re-scan's low-risk launcher rows ──────────────────────────────────────────────────────────────────
const cls = (page, sel) => page.evaluate((s) => document.querySelector(s).className, sel);

// IX re-scan launcher.serial_list (launcher.html:771-866 scan): identify answering {attached:true} is 'connected right
// now' and lit; an identify request that fails, or answers something that is not JSON, leaves the bare chip label; the
// selected row's panel follows the in-place upgrade; FTDI, a description and a bare port are labelled by what they are,
// and macOS's debug, Bluetooth and wlan nodes are skipped.
test('intellex.ui_launcher_identify_states identify\'s other answers upgrade the serial rows in place', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1280, height: 1000 });
  const ports = [
    { path: 'COM81', description: 'USB Serial Device (COM81)', vid: 0x303A, pid: 0x1001, serial_number: '' },
    { path: 'COM82', description: 'USB Serial Device (COM82)', vid: 0x303A, pid: 0x1001, serial_number: '' },
    { path: 'COM83', description: 'USB Serial Device (COM83)', vid: 0x303A, pid: 0x1001, serial_number: '' },
    { path: 'COM84', description: 'FT232R USB UART (COM84)', vid: 0x0403, pid: 0x6001, serial_number: '' },
    { path: 'COM85', description: 'HIL Serial Thing', vid: null, pid: null, serial_number: '' },
    { path: 'COM86', description: '', vid: null, pid: null, serial_number: '' },
    { path: '/dev/cu.debug-console', description: 'n/a', vid: null, pid: null, serial_number: '' },
    { path: '/dev/cu.Bluetooth-Incoming-Port', description: 'n/a', vid: null, pid: null, serial_number: '' },
    { path: '/dev/cu.wlan-debug', description: 'n/a', vid: null, pid: null, serial_number: '' },
  ];
  let release, release82;
  const held = new Promise((r) => { release = r; });
  const held82 = new Promise((r) => { release82 = r; });
  await context.route(/\/_api\/ports(\?|$)/, (r) => r.fulfill({ json: { ports } }));
  const calls = await hostGuard(context, rec, {
    discover: (route) => route.fulfill({ json: { candidates: [] } }),
    identify: async (route, req) => {
      const port = req.postDataJSON().port;
      if (port === 'COM81') { await held; return route.fulfill({ json: { ok: true, attached: true, version: null } }); }
      if (port === 'COM82') { await held82; return route.abort('failed'); }
      return route.fulfill({ status: 500, contentType: 'text/plain', body: 'HIL: not JSON' });
    },
  });
  await page.goto('/_launcher', { waitUntil: 'load' });
  await expect.poll(async () => (await rows(page, 'serial')).map((r) => r.sub), { timeout: 10_000 }).toEqual([
    'Espressif native USB — asking…', 'Espressif native USB — asking…', 'Espressif native USB', 'FTDI bridge',
    'HIL Serial Thing', 'serial port']);
  expect((await rows(page, 'serial')).map((r) => r.title), 'the rows (debug, Bluetooth and wlan nodes skipped)')
    .toEqual(['COM81', 'COM82', 'COM83', 'COM84', 'COM85', 'COM86']);
  expect(calls.filter((c) => c.name === 'identify').map((c) => c.body.port).sort(), 'identify asked of 303A only')
    .toEqual(['COM81', 'COM82', 'COM83']);
  await expect.poll(() => text(page, '#selTitle'), { timeout: 10_000, message: 'auto-select' }).toBe('COM81');
  expect(await text(page, '#selSub'), 'the panel while COM81 is still being asked').toBe('Espressif native USB — asking…');
  release();
  await expect.poll(async () => (await rows(page, 'serial'))[0], { timeout: 10_000 })
    .toEqual({ title: 'COM81', sub: 'connected right now', up: true, sel: true });
  await expect.poll(() => text(page, '#selSub'), { timeout: 5_000, message: 'the panel did not follow the upgrade' })
    .toBe('connected right now');
  // A selected row whose identify request then fails: the row and the panel drop 'asking…' for the chip label.
  await page.locator('#serial .row', { hasText: 'COM82' }).click();
  expect(await text(page, '#selSub'), 'the panel while COM82 is still being asked').toBe('Espressif native USB — asking…');
  release82();
  await expect.poll(async () => (await rows(page, 'serial'))[1].sub, { timeout: 10_000 }).toBe('Espressif native USB');
  await expect.poll(() => text(page, '#selSub'), { timeout: 5_000, message: 'the panel kept "asking…" after the '
    + 'identify request failed' }).toBe('Espressif native USB');
  expect((await rows(page, 'serial')).map((r) => r.up), 'only the attached port is lit')
    .toEqual([true, false, false, false, false, false]);
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan launcher.wifi_list (launcher.html:681-769 scanWifi, watchForDroid): with nothing that is a droid, one hint
// from the most informative candidate - reachable before routable before the first - with ' — still watching…' while
// the 2 min watch runs; the watch rescans every 4 s and stops at 2 min, its last pass without the suffix. The page's
// clock is Playwright's (page.clock), so 2 minutes pass in moments.
test('intellex.ui_launcher_wifi_watch one hint while watching, and the watch ends after 2 minutes', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  let cands = [
    cand({ host: '10.0.0.9', via: null, routable: false, reachable: false, hint: 'HIL no route' }),
    cand({ host: '192.168.4.1', routable: true, reachable: false, hint: 'HIL routed via 192.168.4.2 but no TCP' }),
  ];
  await context.route(/\/_api\/ports(\?|$)/, (r) => r.fulfill({ json: { ports: [] } }));
  const calls = await hostGuard(context, rec, { discover: (route) => route.fulfill({ json: { candidates: cands } }) });
  const discovers = () => calls.filter((c) => c.name === 'discover').length;
  const empty = () => page.evaluate(() => (document.querySelector('#wifi .empty') || {}).textContent || null);
  await page.clock.install();
  await page.goto('/_launcher', { waitUntil: 'load' });
  await expect.poll(empty, { timeout: 10_000 }).toBe('HIL routed via 192.168.4.2 but no TCP — still watching…');
  expect(await text(page, '#scanstate')).toBe('watching…');
  const n0 = discovers();
  await page.clock.runFor(4_100);
  await expect.poll(discovers, { timeout: 5_000, message: 'the watch did not rescan after 4 s' }).toBe(n0 + 1);
  // A reachable candidate is the more informative one, wherever it is in the list.
  cands = [cands[1], cand({ host: '192.168.4.5', routable: true, reachable: true, hint: 'HIL something on port 80' })];
  await page.clock.runFor(4_100);
  await expect.poll(empty, { timeout: 5_000 }).toBe('HIL something on port 80 — still watching…');
  // Past the 2 minutes: one more pass, then the last one without the suffix, and no more rescans.
  await page.clock.fastForward(120_000);
  await expect.poll(empty, { timeout: 5_000, message: 'the last pass still says it is watching' })
    .toBe('HIL something on port 80');
  await page.waitForTimeout(500);
  const n1 = discovers();
  await page.clock.runFor(30_000);
  await page.waitForTimeout(1_000);
  expect(discovers(), 'the watch kept rescanning after 2 minutes').toBe(n1);
  // Pinned as it is today (a product call): the header keeps 'watching…' after the watch has ended, since the last
  // pass still saw a routable host not answering (scanWifi sets it from what it found, not from the watch).
  expect(await text(page, '#scanstate')).toBe('watching…');
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan launcher.theme_views (launcher.html:521-606): a first run follows the OS (prefers-color-scheme), the mark
// swaps to its light build with the theme, the choice is remembered over the OS, and the layout picker is built from
// VIEWS with WCB the default.
test('intellex.ui_launcher_theme_first_run the first run follows the OS theme and the mark follows the theme', async ({ browser, rec }) => {
  const base = process.env.INTELLEX_URL;
  const look = (p) => p.evaluate(() => ({ theme: document.documentElement.getAttribute('data-theme'),
    mark: new URL(document.querySelector('#top .mark').src).pathname, button: document.getElementById('theme').textContent,
    stored: localStorage.getItem('intellex_theme') }));
  for (const scheme of ['light', 'dark']) {
    const ctx = await browser.newContext({ colorScheme: scheme, baseURL: base });
    try {
      await ctx.route(/\/_api\/ports(\?|$)/, (r) => r.fulfill({ json: { ports: [] } }));
      await hostGuard(ctx, rec, { discover: (route) => route.fulfill({ json: { candidates: [] } }) });
      const p = await ctx.newPage();
      const errors = [];
      p.on('pageerror', (e) => errors.push(String(e && e.message || e)));
      await p.goto('/_launcher', { waitUntil: 'load' });
      const other = scheme === 'light' ? 'dark' : 'light';
      const mark = (t) => (t === 'light' ? '/_assets/intellex-mark-128-light.png' : '/_assets/intellex-mark-128.png');
      const button = (t) => (t === 'light' ? 'Dark mode' : 'Light mode');
      expect(await look(p), `a first run with the OS ${scheme}`).toEqual({ theme: scheme, mark: mark(scheme),
        button: button(scheme), stored: scheme });
      await p.click('#theme');
      expect(await look(p), `after the toggle (OS ${scheme})`).toEqual({ theme: other, mark: mark(other),
        button: button(other), stored: other });
      await p.reload({ waitUntil: 'load' });
      expect((await look(p)).theme, `the choice is remembered over the OS ${scheme}`).toBe(other);
      if (scheme === 'light') {
        expect(await p.evaluate(() => [...document.querySelectorAll('#openwith .pick')].map((b) => [b.dataset.view,
          b.querySelector('b').textContent, b.getAttribute('aria-pressed')])), 'the picker, from VIEWS').toEqual([
          ['nc', 'NaviCore', 'false'], ['wcb', 'WCB', 'true'], ['split', 'Side by side', 'false'],
          ['sep', 'Separate windows', 'false']]);
      }
      expect(errors, 'page errors').toEqual([]);
    } finally {
      await ctx.close();
    }
  }
});

// IX re-scan launcher.maintenance_actions (launcher.html:929-941, :1128-1141, :1175-1191): Update tools, Download
// firmware and Download docs POST their route with the button disabled meanwhile, say what landed - success, partial
// and failure in the error style - and re-check; Rescan scans again and re-runs the checks.
test('intellex.ui_launcher_maintenance_buttons Update tools, Download firmware, Download docs and Rescan report and re-check', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1280, height: 1000 });
  const replies = { 'update-webui': [], 'update-firmware': [], 'update-wiki': [] };
  let hold = null;
  const answer = (name) => async (route) => {
    if (hold) await hold.promise;
    const r = replies[name].shift() || { json: { ok: true } };
    return r.body !== undefined ? route.fulfill({ status: r.status || 500, contentType: 'text/plain', body: r.body })
                                : route.fulfill({ json: r.json });
  };
  await context.route(/\/_api\/ports(\?|$)/, (r) => r.fulfill({ json: { ports: [] } }));
  const calls = await hostGuard(context, rec, {
    discover: (route) => route.fulfill({ json: { candidates: [] } }),
    'update-webui': answer('update-webui'), 'update-firmware': answer('update-firmware'),
    'update-wiki': answer('update-wiki'),
  });
  const gets = [];
  page.on('request', (r) => { if (r.method() === 'GET') gets.push(new URL(r.url()).pathname); });
  const count = (p) => gets.filter((g) => g === p).length;
  const status = () => page.evaluate(() => { const s = document.getElementById('status'); return [s.textContent, s.className]; });
  await page.goto('/_launcher', { waitUntil: 'load' });
  await page.evaluate(() => { document.getElementById('maint').open = true; });
  await expect.poll(() => count('/_api/wiki'), { timeout: 10_000 }).toBeGreaterThan(0);

  async function press(button, route, reply, recheck, busyText) {
    replies[route].push(reply);
    let open;
    hold = { promise: new Promise((r) => { open = r; }) };
    const n = calls.filter((c) => c.name === route).length;
    const before = count(recheck);
    await page.click('#' + button);
    await expect.poll(() => calls.filter((c) => c.name === route).length, { timeout: 5_000,
      message: `#${button} did not POST /_api/${route}` }).toBe(n + 1);
    expect(await page.evaluate((b) => document.getElementById(b).disabled, button), `#${button} while it runs`)
      .toBe(true);
    expect((await status())[0], `#${button}'s status while it runs`).toBe(busyText);
    open();
    hold = null;
    await expect.poll(() => page.evaluate((b) => document.getElementById(b).disabled, button), { timeout: 5_000 })
      .toBe(false);
    await expect.poll(() => count(recheck), { timeout: 5_000, message: `#${button} did not re-check ${recheck}` })
      .toBeGreaterThan(before);
    return status();
  }
  const UPD = 'downloading the latest config tool and Wizard…';
  expect(await press('update', 'update-webui', { json: { ok: true, version: 'HILA', wcbVersion: 'HILB' } },
    '/_api/webui-version', UPD)).toEqual(['updated — config tool HILA, Wizard HILB', 'status']);
  expect(await press('update', 'update-webui', { json: { ok: false, error: 'HIL E', version: 'HILA' } },
    '/_api/webui-version', UPD)).toEqual(['HIL E (now: config tool HILA)', 'status err']);
  expect(await press('update', 'update-webui', { json: { ok: false } }, '/_api/webui-version', UPD))
    .toEqual(['update failed', 'status err']);
  expect(await press('update', 'update-webui', { json: { ok: true } }, '/_api/webui-version', UPD))
    .toEqual(['updated', 'status']);
  const broken = await press('update', 'update-webui', { body: 'HIL: not JSON' }, '/_api/webui-version', UPD);
  expect(broken[1]).toBe('status err');
  expect(broken[0], 'a reply that is not JSON').toMatch(/^update failed: /);

  const FW = 'downloading firmware for offline use — this can take a minute…';
  expect(await press('updatefw', 'update-firmware', { json: { ok: true } }, '/_api/firmware', FW))
    .toEqual(['firmware cached — safe to go offline now', 'status']);
  expect(await press('updatefw', 'update-firmware', { json: { ok: false, error: 'HIL FW' } }, '/_api/firmware', FW))
    .toEqual(['firmware update incomplete: HIL FW', 'status err']);
  expect(await press('updatefw', 'update-firmware', { json: { ok: false } }, '/_api/firmware', FW))
    .toEqual(['firmware update incomplete: see the log', 'status err']);

  const WIKI = 'downloading the wikis…';
  expect(await press('updatewiki', 'update-wiki', { json: { ok: true } }, '/_api/wiki', WIKI))
    .toEqual(['docs updated', 'status']);
  expect(await press('updatewiki', 'update-wiki', { json: { ok: false, error: 'HIL W' } }, '/_api/wiki', WIKI))
    .toEqual(['docs update incomplete: HIL W', 'status err']);
  const wb = await press('updatewiki', 'update-wiki', { body: 'HIL: not JSON' }, '/_api/wiki', WIKI);
  expect(wb[1]).toBe('status err');
  expect(wb[0]).toMatch(/^could not download the docs: /);

  // Rescan: both lists and every check again.
  const before = Object.fromEntries(['/_api/ports', '/_api/webui-version', '/_api/firmware', '/_api/log']
    .map((p) => [p, count(p)]));
  const d0 = calls.filter((c) => c.name === 'discover').length;
  await page.click('#rescan');
  for (const p of Object.keys(before)) {
    await expect.poll(() => count(p), { timeout: 5_000, message: `Rescan did not ask ${p} again` })
      .toBeGreaterThan(before[p]);
  }
  await expect.poll(() => calls.filter((c) => c.name === 'discover').length, { timeout: 5_000 }).toBeGreaterThan(d0);
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan launcher.branch_picker (launcher.html:1007-1090): the harness seeds a branch-list cache that lacks the
// WCB branch it set (hil-gone), offline. The note calls it out; Refresh asks /_api/branch-list?refresh=1 and says the
// list's error in the status line in the error style; a refresh that works says 'branch lists updated' and the note
// goes; a list request that fails is 'could not ask the host'.
test('intellex.ui_launcher_branch_refresh Refresh forces the list and says why it could not, and a vanished branch is called out', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1280, height: 1000 });
  await context.route(/\/_api\/ports(\?|$)/, (r) => r.fulfill({ json: { ports: [] } }));
  await hostGuard(context, rec, { discover: (route) => route.fulfill({ json: { candidates: [] } }) });
  const lists = [];
  page.on('request', (r) => { if (new URL(r.url()).pathname === '/_api/branch-list') lists.push(new URL(r.url()).search); });
  const status = () => page.evaluate(() => { const s = document.getElementById('status'); return [s.textContent, s.className]; });
  await page.goto('/_launcher', { waitUntil: 'load' });
  await page.evaluate(() => { document.getElementById('maint').open = true; });
  const GONE = 'WCB is set to a branch that is no longer in the list — flashing it will fail. Pick another.';
  await expect.poll(() => text(page, '#br-note'), { timeout: 10_000 }).toContain(GONE);
  const note = await text(page, '#br-note');
  expect(note, 'the non-main warning').toContain('Not on main: WCB → hil-gone');
  expect(note, 'the offline reason').toContain('Branch list: offline (');
  expect(await page.evaluate(() => [[...document.querySelectorAll('#br-wcb option')].map((o) => o.value),
    document.getElementById('br-wcb').value]), 'the WCB dropdown').toEqual([['hil-gone', 'main', 'hil-feat', ':other:'],
    'hil-gone']);
  expect(lists, 'the first fill is not forced').not.toContain('?refresh=1');

  await page.click('#br-refresh');
  await expect.poll(() => lists.includes('?refresh=1'), { timeout: 5_000, message: 'Refresh did not force the list' })
    .toBe(true);
  await expect.poll(status, { timeout: 10_000 }).toEqual([expect.stringMatching(/^branch list: offline \(/),
    'status err']);
  expect(await page.evaluate(() => document.getElementById('br-refresh').disabled)).toBe(false);

  // A refresh that comes back clean (fulfilled here): the status says so and the vanished-branch note goes.
  await context.route(/\/_api\/branch-list(\?.*)?$/, (r) => r.fulfill({ json: { navicore: ['main'],
    wcb: ['hil-gone', 'main'], cached: false, error: '', missing: [] } }));
  await page.click('#br-refresh');
  await expect.poll(status, { timeout: 10_000 }).toEqual(['branch lists updated', 'status']);
  expect(await text(page, '#br-note')).not.toContain(GONE);
  // A list request that fails outright.
  await context.unroute(/\/_api\/branch-list(\?.*)?$/);
  await context.route(/\/_api\/branch-list(\?.*)?$/, (r) => r.abort('failed'));
  await page.click('#br-refresh');
  await expect.poll(status, { timeout: 10_000 }).toEqual([expect.stringMatching(/^branch list: could not ask the host \(/),
    'status err']);
  await context.unroute(/\/_api\/branch-list(\?.*)?$/);
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan launcher.log_docs_version (launcher.html:1116-1173): with docs stored, Docs asks the host for a second
// window on /wiki/ and opens it itself when there is none (host.py has no window backend: a popup), and opens nothing
// when the host made one; the header shows the build as /_api/version says it.
test('intellex.ui_launcher_docs_version Docs opens /wiki/ in a second window and the header shows the build', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1280, height: 1000 });
  await context.route(/\/_api\/ports(\?|$)/, (r) => r.fulfill({ json: { ports: [] } }));
  await hostGuard(context, rec, { discover: (route) => route.fulfill({ json: { candidates: [] } }) });
  const asked = [];
  let backend = false;
  await context.route(/\/_api\/open-window(\?|$)/, (route, req) => {
    asked.push(req.postDataJSON());
    rec.fulfilled.add(req.url());
    return backend ? route.fulfill({ json: { ok: true } })
                   : route.fulfill({ status: 501, json: { ok: false, error: 'no window backend' } });
  });
  await page.goto('/_launcher', { waitUntil: 'load' });
  const v = await (await page.request.get('/_api/version')).json();
  const bits = [v.commit, v.built].filter(Boolean).join(', ');
  await expect.poll(() => text(page, '#appver'), { timeout: 10_000 }).toBe('v' + v.version + (bits ? ` (${bits})` : ''));
  await expect.poll(() => page.evaluate(() => document.getElementById('opendocs').disabled), { timeout: 10_000,
    message: 'Docs is disabled with the crafted wiki stored' }).toBe(false);
  const [popup] = await Promise.all([context.waitForEvent('page', { timeout: 10_000 }), page.click('#opendocs')]);
  await expect.poll(() => new URL(popup.url()).pathname, { timeout: 10_000 }).toMatch(/^\/wiki\/(intellex\/)?$/);
  expect(asked, 'what Docs asked the host for').toEqual([{ url: '/wiki/', title: 'Intellex Docs' }]);
  await popup.close();
  backend = true;
  const popups = [];
  context.on('page', (p) => popups.push(p.url()));
  await page.click('#opendocs');
  await expect.poll(() => asked.length, { timeout: 5_000 }).toBe(2);
  await page.waitForTimeout(1_500);
  expect(popups, 'Docs opened a window itself although the host made one').toEqual([]);
  expect(new URL(page.url()).pathname, 'Docs navigated the launcher').toBe('/_launcher');
  expect(rec.errors, 'page errors').toEqual([]);
});

// IX re-scan launcher.back_offline_reload (launcher.html:1196-1232 offerOfflineIfEmpty): with no port and no droid the
// status line points at 'Open the tool anyway' - once both lists have answered, not after the serial half alone.
test('intellex.ui_launcher_no_droid with nothing found the chooser offers the tool without a board', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  let release;
  const held = new Promise((r) => { release = r; });
  await context.route(/\/_api\/ports(\?|$)/, (r) => r.fulfill({ json: { ports: [] } }));
  await hostGuard(context, rec, { discover: async (route) => { await held; return route.fulfill({ json: { candidates: [] } }); } });
  await page.goto('/_launcher', { waitUntil: 'load' });
  await expect.poll(() => text(page, '#serial .empty'), { timeout: 10_000 }).toBe('no serial ports found');
  await page.waitForTimeout(1_000);
  expect(await text(page, '#status'), 'the status before the WiFi list answered').not.toContain('No droid found');
  release();
  await expect.poll(() => text(page, '#status'), { timeout: 10_000 }).toBe('No droid found. You can still open the tool '
    + 'on its own — panels, saved configs and editing all work without a board.');
  expect(await cls(page, '#status')).toBe('status');
  expect(await text(page, '#wifi .empty')).toBe('no droid found');
  expect(await page.evaluate(() => document.getElementById('go').disabled), 'Connect with nothing to pick').toBe(true);
  expect(rec.errors, 'page errors').toEqual([]);
});
