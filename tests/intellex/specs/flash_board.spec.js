// Flashing through Intellex (INTELLEX.md IX-WP10). Each test is started by the harness test of the same id
// (tests/hil/suites/s36_intellex_flash.py), which seeds the staged host's firmware cache with the bench image the board
// already runs (offline, test branch hil-bench), hands the host that board's COM port alone and, afterwards, proves the
// board came back as it was: its version, the slot it runs, its config (config_guard / nc_guard), and a boot heard on
// the mesh. The flash itself runs the way a user runs it: the Wizard's boardGo(<W2's slot>, {mode}) - what its Go button
// runs - or the NaviCore config tool's Update Firmware button; the shim hands both to the host's esptool. The spec
// follows /_api/flash-status and then asks the harness to judge the host's flash log (hook flash_done), where the regions
// esptool wrote and verified are read.
//
// Nothing here reads a config value or the NaviCore tool's #terminal-output (D-NC5).
const { test, expect, skipUnlessHost } = require('../lib/fixtures');
const B = require('../lib/board');

skipUnlessHost(test);
test.beforeEach(() => {
  test.skip(!B.hil.present, 'board tests run under the HIL harness: python tests/hil/run.py "intellex.flash_*"');
});

const args = async () => (await B.hil.context()).args || {};
const LABEL = { update: 'updated', flash: 'flashed', factory: 'factory reset' };   // boardGo's own toast (app.js)
const BUSY = 'a flash is already running';                                         // host.py _claim_port_for_flash

// The Wizard on W2 through Intellex: auto-connected and pulled, the mesh routed through W2 (W1 pulled there), then the
// flash in args.mode as boardGo runs it. args.concurrent: once the flash runs, a second one is asked for from the page -
// the NaviCore tool's route and the Wizard's again - and both must be refused while nothing else starts.
async function wizardFlash(page, context, rec, id) {
  test.setTimeout(900_000);                  // the pull, the mesh settling, the flash and the reconnect; harness: 960 s
  page.setDefaultTimeout(15_000);
  page.on('dialog', (d) => d.accept());      // nothing on this path asks; if the Wizard ever does, a user says yes
  const a = await args();
  const g = await B.flashGuard(context, rec);
  await B.openWizard(page);
  await B.recordToasts(page);
  await B.spyBoardPulls(page);
  const n = a.wcb;
  expect(await B.waitPulled(page, n, 60_000), `W${n} pulled into slot ${n} with no click`).toBe(n);
  const left = await B.meshSettled(page, a.boards, 120_000);
  if (left.length) await B.hil.note(`${id}: WCB${left.join(', ')} not yet pulled through W${n}; flashing anyway`);
  const pulls0 = await page.evaluate(() => window.__hilBoardPulls.length);
  const toasts0 = await page.evaluate(() => window.__hilToasts.length);
  await page.evaluate(({ n, mode, push }) => {
    window.__hilGo = 'running';
    Promise.resolve(boardGo(n, { mode, pushConfig: push })).then(() => { window.__hilGo = 'done'; },
      (e) => { window.__hilGo = `error: ${String((e && e.message) || e)}`; });
  }, { n, mode: a.mode, push: !!a.push });
  const conc = [];
  const t0 = Date.now();
  const f = await B.followFlash({
    during: a.concurrent ? async () => {
      for (const route of ['/_api/flash', '/_api/flash-wcb']) {
        conc.push(await page.evaluate(async (route) => {
          const r = await fetch(route, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
          const j = await r.json().catch(() => ({}));
          return { route, status: r.status, ok: j.ok, error: j.error };
        }, route));
      }
    } : null,
    stopIf: async (saw) => {
      if (saw) return null;
      const st = await page.evaluate(() => window.__hilGo);
      if (st === 'running') return null;
      const said = await page.evaluate((k) => window.__hilToasts.slice(k).map((t) => `${t.type}: ${t.message}`), toasts0);
      return `boardGo ended (${st}) before any flash started; its toasts: ${said.join(' | ') || 'none'}`;
    },
  });
  const flashS = Math.round((Date.now() - t0) / 1000);
  // Judge the host's flash first: a flash that failed (esptool never reached the ROM loader, run 20260929-203948)
  // leaves nothing to reconnect to, and waiting out the re-pull would only say that.
  const j = await B.hil.hook('flash_done', { percents: f.percents });
  expect(j.problems, `the host's flash log (${flashS} s)`).toEqual([]);
  // boardGo returns once it has reconnected the board and (outside the guided setup) started pulling it again.
  await expect.poll(() => page.evaluate(() => window.__hilGo), { timeout: 180_000,
    message: 'boardGo did not finish after the flash' }).toBe('done');
  if (a.mode !== 'factory') {
    await expect.poll(() => page.evaluate(({ n, k }) => window.__hilBoardPulls.slice(k).some((p) => p.n === n && p.ok === true)
      && !!boardConnections[n]?.isConnected?.(), { n, k: pulls0 }), { timeout: 120_000,
      message: `the Wizard did not pull W${n} again over the reconnected link` }).toBe(true);
  }
  const toasts = await page.evaluate((k) => window.__hilToasts.slice(k), toasts0);
  const said = toasts.map((t) => `${t.type}: ${t.message}`).join(' | ');
  expect(toasts.filter((t) => t.type === 'success' && t.message.startsWith(`WCB ${n} firmware ${LABEL[a.mode]} in`)).length,
    `the Wizard's own verdict (its toasts: ${said})`).toBe(1);
  expect(toasts.filter((t) => /Flash failed/.test(t.message)).map((t) => t.message), 'a flash failure toast').toEqual([]);
  if (a.concurrent) {
    expect(conc.map((c) => [c.route, c.status, c.ok, c.error]), 'a second flash asked for while one runs')
      .toEqual([['/_api/flash', 409, false, BUSY], ['/_api/flash-wcb', 409, false, BUSY]]);
  }
  expect(g.flashes.filter((x) => x.status === 200).map((x) => [x.name, x.body]), 'the flashes the host started')
    .toEqual([['flash-wcb', { appOnly: a.mode === 'update', eraseNvs: a.mode === 'factory' }]]);
  await B.hil.note(`${id}: the host's flash ran about ${flashS} s; progress read ${JSON.stringify(j.facts.percents)}; ` +
    `${j.facts.written.length} region(s) written (${j.facts.written.join(', ')}), ${j.facts.verified} verified`);
  B.expectClean(rec, 'the Wizard');
}

test('intellex.flash_w2_update the Wizard\'s Update FW through Intellex writes W2\'s own image into app0, offline from the cache',
  async ({ page, context, rec }) => {
  await wizardFlash(page, context, rec, 'intellex.flash_w2_update');
});

test('intellex.flash_w2_full the Wizard\'s Flash through Intellex writes bootloader, table and app from one build, NVS kept',
  async ({ page, context, rec }) => {
  await wizardFlash(page, context, rec, 'intellex.flash_w2_full');
});

test('intellex.flash_one_at_a_time while the Wizard\'s Update runs through Intellex a second flash is refused with 409',
  async ({ page, context, rec }) => {
  await wizardFlash(page, context, rec, 'intellex.flash_one_at_a_time');
});

test('intellex.flash_w2_factory the Wizard\'s Factory Reset through Intellex writes the image and erases W2\'s NVS',
  async ({ page, context, rec }) => {
  await wizardFlash(page, context, rec, 'intellex.flash_w2_factory');
});

const button = (page, id) => page.evaluate((i) => {
  const b = document.getElementById(i);
  return b ? { disabled: b.disabled, title: b.title, wired: !!b.__intellexWired } : null;
}, id);

// The NaviCore config tool's Update Firmware on NaviCore's own COM port: the shim takes the click (capture phase) and
// POSTs /_api/flash {eraseNvs:false}; the host writes app0 and erases otadata, esptool restarts NaviCore, the host
// reattaches the port, and the shim's watchLink reconnects the tool, which PINGs again.
test('intellex.flash_navicore_app the config tool\'s Update Firmware through Intellex writes NaviCore\'s own image into app0',
  async ({ page, context, rec }) => {
  test.setTimeout(600_000);                         // the harness's watchdog is 660 s (s36)
  page.setDefaultTimeout(15_000);
  const a = await args();
  const g = await B.flashGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  const s = await B.ncHandshake(page, { timeout: 60_000 });
  expect(s.status).toBe('Connected ✓');
  expect(s.fw, 'the version before the flash').toBe(a.version);
  await expect.poll(() => button(page, 'btn-fw-flash'), { timeout: 10_000, message: 'Update Firmware: wired to the host' })
    .toEqual(expect.objectContaining({ disabled: false, wired: true }));
  // The button is on the Hardware Setup dialog's Firmware tab (tests/wizard/specs/navicore/firmware.spec.js).
  await page.locator('#btn-hwsetup').click();
  await page.locator('#cfg-tabs .cfg-tab[data-tab="firmware"]').click();
  await expect(page.locator('#cfg-pane-firmware')).toHaveClass(/active/);
  const t0 = Date.now();
  await page.locator('#btn-fw-flash').click();
  const f = await B.followFlash({ timeout: 300_000 });
  await expect(page.locator('#fw-status'), 'the tool\'s own verdict').toHaveText(
    `Update Firmware complete ✓ — board is running ${a.version}`, { timeout: 20_000 });
  await expect.poll(async () => {
    const x = await B.ncState(page);
    return x.status === 'Connected ✓' && x.fw === a.version && x.connected;
  }, { timeout: 150_000, message: 'the tool did not reconnect to NaviCore after the flash' }).toBe(true);
  expect(g.flashes.map((x) => [x.name, x.status, x.body]), 'the flashes the host started')
    .toEqual([['flash', 200, { eraseNvs: false }]]);
  const j = await B.hil.hook('flash_done', { percents: f.percents });
  await B.hil.note(`intellex.flash_navicore_app: back and PINGing ${Math.round((Date.now() - t0) / 1000)} s after the ` +
    `click; progress read ${JSON.stringify(j.facts.percents)}; ${j.facts.written.join(', ')} written, ${j.facts.verified} ` +
    `verified; ${j.facts.deprecated} 'Deprecated' line(s) (INTELLEX.md finding 14)`);
  expect(j.problems, 'the host\'s flash log').toEqual([]);
  expect(B.unaskedWrites(link), 'JSON the tool sent that is not a read').toEqual([]);
  B.expectClean(rec, 'the config tool');
});
