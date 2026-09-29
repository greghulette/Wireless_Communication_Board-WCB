// The NaviCore config tool through a staged Intellex host (INTELLEX.md IX-WP8): attached to NaviCore's own COM port for
// the nc_* tests, and to W1's for nc_via_usb_doorway (finding 4). Each is started by the harness test of the same id
// (tests/hil/suites/s34_intellex_tools.py), which releases that port to the host and, afterwards, checks the board never
// restarted - from a raw /_link client of its own, NaviCore's uptime and W1's view of the mesh - and puts NaviCore's
// monitor and debug flags back (the tool's connect starts both, RAM only). The tool connects on its own: the shim calls
// its connectDirect() once the host is attached (intellex_shim.js:396-450).
//
// D-NC5: nothing here reads #terminal-output or quotes a config value - the CONFIG line carries the mesh and AP
// passwords. Config fields reach a spec from the harness by name, only non-secret scalars; the byte-for-byte comparison
// of the CONFIG line is the harness's own, on its raw /_link client.
const { test, expect, skipUnlessHost } = require('../lib/fixtures');
const B = require('../lib/board');

skipUnlessHost(test);
test.beforeEach(() => {
  test.skip(!B.hil.present, 'board tests run under the HIL harness: python tests/hil/run.py "intellex.nc_*"');
});

const args = async () => (await B.hil.context()).args || {};
// The titles the shim gives the flash buttons once they are wired to the host's esptool (intellex_shim.js:539-544).
const NATIVE_FLASH = "Update via the host's native esptool. Saved configuration (NVS) is preserved.";
const NATIVE_WIPE = "Full wipe via the host's native esptool. ERASES saved configuration (NVS).";

const button = (page, id) => page.evaluate((i) => {
  const b = document.getElementById(i);
  return b ? { disabled: b.disabled, title: b.title, text: (b.textContent || '').trim(), wired: !!b.__intellexWired }
           : null;
}, id);

test('intellex.nc_autoconnect through Intellex the config tool connects to NaviCore on its own, directly',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  const g = await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  const s = await B.ncHandshake(page);
  expect(s.status, 'the status line').toBe('Connected ✓');
  expect(s.viaWcbActive, 'Via WCB on a direct link').toBe(false);
  expect(s.pongEpoch, 'the direct PING was answered (no fallback to Via WCB)').toBe(1);
  expect(s.fw, 'the firmware version from the PONG').toBe(a.version);
  await expect.poll(async () => (await B.ncState(page)).transport, { timeout: 6_000,
    message: 'the transport label beside the status' }).toBe(`· USB ${a.com} ▾`);
  await expect.poll(() => button(page, 'btn-fw-flash'), { timeout: 10_000, message: 'Update Firmware: wired to the host' })
    .toEqual(expect.objectContaining({ disabled: false, title: NATIVE_FLASH, wired: true }));
  await expect.poll(() => button(page, 'btn-fw-wipe'), { timeout: 10_000, message: 'Full Wipe: wired to the host' })
    .toEqual(expect.objectContaining({ disabled: false, title: NATIVE_WIPE, wired: true }));
  const ota = await button(page, 'btn-fw-ota');
  expect(ota && ota.text, 'the OTA button').toContain('Update over USB (OTA)');
  expect(ota.text, 'the OTA button names WiFi on a USB link').not.toContain('WiFi');
  // openPortAndStart deasserts both lines right after open() (index.html:4528-4540); through the host they reach the port.
  const both = g.signals.filter((x) => x.body && x.body.dataTerminalReady === false && x.body.requestToSend === false);
  expect(both.length, "the tool's setSignals({DTR:false, RTS:false}) reached the host").toBeGreaterThan(0);
  expect(g.signals.filter((x) => x.status !== 200 || x.ok !== true).map((x) => `${JSON.stringify(x.body)}->${x.status}`),
    'setSignals calls the host did not carry out').toEqual([]);
  expect(B.unaskedWrites(link), 'JSON the tool sent on its own that is not a read').toEqual([]);
  B.expectClean(rec, 'the config tool');
});

test('intellex.nc_reload_no_reset three F5 reloads of the config tool through Intellex, the PONG the same each time',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  let s = await B.ncHandshake(page);
  expect(s.status).toBe('Connected ✓');
  expect(s.fw, 'the firmware version from the PONG').toBe(a.version);
  for (let i = 1; i <= 3; i++) {
    await B.pressF5(page);
    s = await B.ncHandshake(page);
    expect(s.status, `after F5 ${i}`).toBe('Connected ✓');
    expect(s.fw, `after F5 ${i}: the PONG's version`).toBe(a.version);
    expect([s.viaWcbActive, s.pongEpoch], `after F5 ${i}: direct, answered at once`).toEqual([false, 1]);
  }
  expect(link.sockets, 'a /_link per page load').toBeGreaterThanOrEqual(4);
  expect(B.unaskedWrites(link), 'JSON the tool sent on its own that is not a read').toEqual([]);
  B.expectClean(rec, 'the config tool');
  // The reason Intellex exists (its CLAUDE.md, 'Why native serial'): the harness's own /_link client holds every byte
  // NaviCore printed across the reloads - no banner, no reset reason - and its uptime kept counting.
});

test('intellex.nc_config_matches the config the tool loaded through Intellex is NaviCore\'s, field by field',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  const s = await B.ncHandshake(page);
  expect(s.status).toBe('Connected ✓');
  const got = await page.evaluate((paths) => {
    const pick = (o, p) => p.split('.').reduce((x, k) => (x == null ? x : x[k]), o);
    const out = {}, base = {};
    for (const p of paths) { out[p] = pick(config, p); base[p] = pick(_configBaseline, p); }
    return { out, base, diff: Object.keys(_diffConfigBranches(config, _configBaseline)) };
  }, Object.keys(a.fields));
  // Scalars only: the tool fills what GET_CONFIG leaves out (an empty mapping is never printed, rc_config.h:1262), so
  // counts of mappings or Maestro slots are the tool's, not the board's. The whole line is the harness's comparison.
  expect(Object.keys(a.fields).length, 'the harness sent no fields to compare').toBeGreaterThan(0);
  expect(got.out, "the tool's config, named non-secret fields").toEqual(a.fields);
  expect(got.base, "the tool's baseline, the same fields").toEqual(a.fields);
  expect(got.diff, 'branches a Save right after connecting would send').toEqual([]);
  expect(B.unaskedWrites(link), 'JSON the tool sent on its own that is not a read').toEqual([]);
  B.expectClean(rec, 'the config tool');
});

test('intellex.nc_setsignals the tool\'s setSignals(DTR false, RTS false) through Intellex gets 200 and resets nothing',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await args();
  const g = await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  await B.ncHandshake(page);
  const n0 = g.signals.length;
  expect(g.signals.some((x) => x.body && x.body.dataTerminalReady === false && x.body.requestToSend === false),
    "the connect's own setSignals reached the host").toBe(true);
  // The same call again from the page, on the port the shim gives the tool.
  await page.evaluate(async () => {
    const p = await navigator.serial.requestPort();
    await p.setSignals({ dataTerminalReady: false, requestToSend: false });
  });
  await expect.poll(() => g.signals.length, { timeout: 5_000 }).toBeGreaterThan(n0);
  expect(g.signals.slice(n0), "the host's answer").toEqual([{ body: { dataTerminalReady: false, requestToSend: false },
                                                             status: 200, ok: true }]);
  expect(g.signals.filter((x) => x.status !== 200 || x.ok !== true).length, 'a setSignals the host refused').toBe(0);
  // NaviCore still answers on the same link a few seconds on.
  await page.waitForTimeout(3000);
  await page.evaluate(() => { _pongSeen = false; return sendJSON({ type: 'PING' }); });
  await page.waitForFunction(() => _pongSeen === true, null, { timeout: 5_000 });
  expect(B.unaskedWrites(link), 'JSON the tool sent on its own that is not a read').toEqual([]);
  B.expectClean(rec, 'the config tool');
});

// Opt-in intellex_nc_save, inside nc_guard (the harness proves NaviCore's config byte-identical afterwards). Save with no
// edits sends nothing at all (saveConfigToBoard's no-diff return, index.html:16911-16925), so the save round trip the
// plan asks for is made with one field: the telemetry rate (chRateHz, the rc_ch monitor rate; nothing that moves) up
// or down by one, then back. Each Save is a SET_CONFIG carrying that branch alone, and must be ACKed (the baseline only
// advances on the ACK, index.html:9616-9624).
test('intellex.nc_save_unchanged Save through Intellex: no edit sends nothing, a change saved and undone is ACKed',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  const s = await B.ncHandshake(page);
  expect(s.status).toBe('Connected ✓');
  await page.locator('#btn-hwsetup').click();
  await expect(page.locator('#push-budget')).toHaveText('No changes to save');
  const n0 = link.sent.length;
  await page.locator('#btn-hwsetup-save').click();
  await expect.poll(() => B.ncToasts(page)).toContainEqual(expect.stringContaining('No changes to save'));
  await page.waitForTimeout(1000);
  expect(link.sent.slice(n0).filter((x) => x.type === 'SET_CONFIG').length, 'a Save with no edit sent a SET_CONFIG')
    .toBe(0);
  const v = await page.evaluate(() => config.chRateHz);
  const other = v >= 20 ? v - 1 : v + 1;
  for (const val of [other, v]) {
    await page.evaluate((val) => {
      const el = document.getElementById('chrate-input');
      el.value = String(val);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }, val);
    // Not read back from #push-budget: its readout leaves the numeric fields out (_pushBudgetInfo, index.html:16671-
    // 16676); Save itself reads the slider (saveConfigToBoard, :16868-16872).
    const n1 = link.sent.length;
    await page.locator('#btn-hwsetup-save').click();
    await page.waitForFunction(() => _pendingSaveBaseline === null && !_pendingSaveTimer, null, { timeout: 20_000 });
    expect(await page.evaluate(() => _configBaseline.chRateHz), `chRateHz ${val}: the save was ACKed (the baseline ` +
      'advanced)').toBe(val);
    const sets = link.sent.slice(n1).filter((x) => x.type === 'SET_CONFIG');
    expect(sets.map((x) => x.keys), `chRateHz ${val}: one SET_CONFIG, carrying that branch alone`).toEqual([['chRateHz']]);
  }
  await expect(page.locator('#push-budget')).toHaveText('No changes to save');
  await page.evaluate(() => closeHwSetup());
  expect(B.unaskedWrites(link, ['SET_CONFIG']), 'JSON the tool sent that is neither a read nor the two saves').toEqual([]);
  B.expectClean(rec, 'the config tool');
});

// (should) INTELLEX.md finding 4. Through W1's COM port the tool is reached through a WCB doorway, and must end up in Via
// WCB, where "Update over USB (OTA)" is off: sent directly, ?OTALOCAL lands on the doorway WCB, not on NaviCore (rule 10's
// hazard over USB). A serial attach reports role "" (host.py:1117-1133), so the shim never forces Via WCB
// (intellex_shim.js:440-444), and the tool accepts any PONG (index.html:9515-9519). W1 broadcasts a bare JSON line to the
// mesh (processBroadcastCommand) and prints NaviCore's unicast PONG back (rc_telemetry.h:2186-2192) only inside its 20 s
// relay window, which a ;w20,{...} line opens (WCB.ino:8019-8021, :5513). So a cold connect falls back to Via WCB as it
// should, and a connect while the window is open - a reload, or the shim's reconnect, within 20 s of the tool's last
// Via-WCB line (its keep-alive PING is every 10 s) - takes the mirrored PONG as a direct one. Both are made here: the
// first connect, then F5 at once. The companion data: whether the direct phase got that PONG (_pongEpoch 1).
test('intellex.nc_via_usb_doorway the config tool reached through W1 over USB ends up in Via WCB, also on a reload',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/', { waitUntil: 'load' });
  // The end of the handshake only (START_MONITOR sent), which is where the transport is decided, not its CONFIG: that
  // crosses the mesh in fragment envelopes paced 150 ms apart, and reaches W1's USB only while its relay window is open.
  const first = await B.ncHandshake(page, { config: false, timeout: 60_000 });
  await B.pressF5(page);
  const second = await B.ncHandshake(page, { config: false, timeout: 60_000 });
  const say = (s) => `viaWcbActive ${s.viaWcbActive}, the direct PING ${s.pongEpoch === 1 ? 'answered (a mirrored mesh ' +
    'PONG)' : 'unanswered'}, '${s.status}', OTA button '${s.ota && s.ota.text}' ${s.ota && s.ota.disabled ? 'off' : 'ON'}`;
  await B.hil.note(`intellex.nc_via_usb_doorway: cold connect: ${say(first)}; reload: ${say(second)}`);
  expect(first.status, 'NaviCore did not answer through W1 at all').toMatch(/^Connected/);
  expect(second.status, 'NaviCore did not answer through W1 at all after the reload').toMatch(/^Connected/);
  expect(B.unaskedWrites(link), 'JSON the tool sent through W1 that is not a read').toEqual([]);
  expect([first.viaWcbActive, second.viaWcbActive], `(should) the tool reached through W1 over USB is in Via WCB after ` +
    `every connect - cold: ${say(first)}; reload: ${say(second)} (INTELLEX.md finding 4)`).toEqual([true, true]);
});
