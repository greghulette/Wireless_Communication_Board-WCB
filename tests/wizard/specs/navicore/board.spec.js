// L2: the config tool against the REAL NaviCore, through the harness's own COM handle (the pipe: lib/navicore/pipe.js,
// hil/bridge.py /serial/*; docs/hil_plan/NAVICORE.md §5.2, NC-WP13). Only under the harness, which runs each inside
// nc_guard and hands the spec what it must not work out for itself in `hilCtx.args` (a free slot, a marker, a label, the
// bench image): python tests/hil/run.py "nctool.board_*". Standalone and in CI these skip.
//
// Nothing here may leak a credential: the CONFIG line carries the mesh and AP passwords, so a spec reads counts,
// booleans and key names only, never a config value, and never the terminal pane wholesale (T.termLines drops CONFIG).
// NaviCore's flash holds Greg's own clips and command library: nothing here writes either.
const fs = require('node:fs');
const { test, expect, hil } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const F = require('../../lib/navicore/firmware');
const { sbus } = require('../../lib/navicore/pipe');

test.beforeEach(({ hilCtx }) => {
  test.skip(!hilCtx || !hilCtx.pipe || hilCtx.device !== 'navicore',
            'L2 runs under the HIL harness with NaviCore piped: python tests/hil/run.py "nctool.board_*"');
});

// Connect -> "Connect via USB" -> the fake port the chooser grants -> the tool's own openPortAndStart handshake
// (index.html:4511-4605) against the real board; the 4 s settle is stepped on the page clock (the pipe never resets
// NaviCore, so there is no boot to wait out).
async function connectPiped(page) {
  await page.locator('#btn-connect').click();
  await page.locator('#connect-modal .connect-opt').first().click();
  await page.waitForFunction(() => window.__hilSerial.isOpen());
  await page.clock.fastForward(4000);
  await page.waitForFunction(() => _configLoaded === true && isMonitoring === true, null, { timeout: 45_000 });
}

// Disconnect as a user does, so NaviCore stops streaming PWM_UPDATE (START_MONITOR has no timeout), then wait until
// every line the page wrote has reached the board.
async function disconnectPiped(page, device) {
  await page.evaluate(() => { if (document.getElementById('hwsetup-modal').classList.contains('open')) closeHwSetup(); });
  await page.locator('#btn-connect').click();
  await page.waitForFunction(() => !port);
  await device.flush();
}

async function openConfigTab(page, tab) {
  if (!(await page.locator('#hwsetup-modal.open').count())) await page.locator('#btn-hwsetup').click();
  await page.locator(`#cfg-tabs .cfg-tab[data-tab="${tab}"]`).click();
}

const branchesToSave = (page) => page.evaluate(() => Object.keys(_diffConfigBranches(config, _configBaseline)));
const sent = (device, type) => device.sentTypes.filter((t) => t === type).length;

test('nctool.board_connect_config the tool connects to the real NaviCore through the pipe (no reset: the harness holds the port), applies its CONFIG, and a Save right after sends nothing', async ({ page, device }) => {
  await T.openTool(page);
  await connectPiped(page);
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
  await disconnectPiped(page, device);
  expect(device.sentTypes.filter((t) => t === 'SET_CONFIG' || t === 'RESET_DEFAULTS')).toEqual([]);
  expect(device.sentTypes).toEqual(expect.arrayContaining(['PING', 'GET_CONFIG', 'START_MONITOR', 'STOP_MONITOR']));
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});

// nct.save.diff on the board. The harness picked a port label key the board has not set and a HIL label, and proves
// afterwards that GET_CONFIG changed by exactly that label; nc_guard puts the config back.
test('nctool.board_save_one_field a port label typed in the Serial Ports tab and saved reaches the real NaviCore as one SET_CONFIG carrying only serialLabels, ACKed by its saveId; a Refresh brings it back as the baseline and a second Save sends nothing', async ({ page, device, hilCtx }) => {
  const { key, label } = hilCtx.args;
  await T.openTool(page);
  await connectPiped(page);
  expect(await branchesToSave(page), 'a Save right after connecting').toEqual([]);
  await openConfigTab(page, 'serial');
  await page.locator(`#slabel-${key}`).fill(label);                  // setSerialLabel as you type (index.html:3784-3791)
  expect(await branchesToSave(page)).toEqual(['serialLabels']);
  await page.locator('#btn-hwsetup-save').click();
  await expect.poll(() => T.toasts(page), { timeout: 15_000 }).toContainEqual(expect.stringContaining('Config saved to NaviCore'));
  expect(await page.evaluate(() => [!!_pendingSaveBaseline, _configUnsaved()]), '[save pending, unsaved edits]').toEqual([false, false]);
  expect(sent(device, 'SET_CONFIG')).toBe(1);
  // Refresh: the board's own copy becomes the baseline (the CONFIG handler, index.html:9539-9550), label included.
  await page.evaluate(() => { closeHwSetup(); window.__hilBase = _configBaseline; });
  await page.locator('#btn-refresh').click();
  await page.waitForFunction(() => _configBaseline && _configBaseline !== window.__hilBase, null, { timeout: 20_000 });
  expect(await page.evaluate(({ key, label }) => (_configBaseline.serialLabels || {})[key] === label, { key, label }),
         'the label is in the CONFIG read back').toBe(true);
  expect(await branchesToSave(page), 'a Save after the Refresh').toEqual([]);
  await page.locator('#btn-hwsetup').click();
  await page.locator('#btn-hwsetup-save').click();
  await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No changes to save'));
  await disconnectPiped(page, device);
  expect(sent(device, 'SET_CONFIG'), 'SET_CONFIG lines written').toBe(1);
  expect(device.sentTypes.filter((t) => t === 'RESET_DEFAULTS')).toEqual([]);
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});

// nct.edit.test_action on the board: testActionRow (index.html:15521-15546) sends TEST_ACTION with the row's current,
// unsaved values; NaviCore dispatches it as a real press would, so the wcb_unicast reaches W1 and ;S2<marker> comes
// out of its S2 port, which the harness bound before the spec. The free slot's editor is opened and closed unsaved.
test("nctool.board_test_action_wire an action row's ▶ Test on the real NaviCore fires its command: a wcb_unicast of ;S2 + a marker to WCB 1 comes out of W1 S2 exactly once, and nothing is saved", async ({ page, device, hilCtx }) => {
  const a = hilCtx.args;
  const w = hil.wire(a.wcb, a.port);
  const want = Buffer.from(`${a.text}\r`);
  await T.openTool(page);
  await connectPiped(page);
  await page.evaluate(({ mode, btn }) => { setEditingMode(mode); openModal(btn); }, a);   // never a Shift-click: that TRIGGERs
  await expect(page.locator('#modal')).toHaveClass(/open/);
  // The row's "Send to": WCB <n>, Serial auto = a wcb_unicast to that board (the dropdowns' sync(), index.html:15168-15192);
  // the command carries its own ;S<port>.
  await page.locator('#fields-1-0-wcbsel').selectOption(String(a.wcb));
  await page.locator('#fields-1-0-portsel').selectOption('auto');
  await page.locator('#fields-1-0-cmd').fill(`;${a.port}${a.text}`);
  expect(await page.evaluate(() => readActionFromUI('1', 0)), 'the action the Test fires')
    .toEqual({ type: 'wcb_unicast', target: String(a.wcb), cmd: `;${a.port}${a.text}` });
  const since = await w.mark();
  await page.locator('#action-1-0 .btn-test-action').click();
  await device.flush();
  await w.expect(want.toString(), since, 5);
  await page.waitForTimeout(1500);                                   // room for a second copy that must not come
  const got = await w.received(since);
  let n = 0;
  for (let i = got.indexOf(want); i >= 0; i = got.indexOf(want, i + 1)) n++;
  expect(n, `copies of the marker on W${a.wcb} ${a.port}`).toBe(1);
  await page.evaluate(() => closeModal());
  expect(await page.evaluate(() => _configUnsaved()), 'Test left an edit behind').toBe(false);
  await disconnectPiped(page, device);
  expect(sent(device, 'TEST_ACTION')).toBe(1);
  expect(device.sentTypes.filter((t) => t === 'SET_CONFIG' || t === 'TRIGGER')).toEqual([]);
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});

// nct.live.pwm_update on the board: the SBUS channel grid (updateChannelsGrid, index.html:11379-11432) shows each
// channel's raw SBUS count from PWM_UPDATE (displayUnit 'sbus' in a fresh profile, fmtVal :3418-3420). The harness
// computed the rx stick's argument for each count (s42 axis_arg) and checked it with #L09; the spec moves the stick
// through the bridge's /sbus route and centres it again whatever happens.
test('nctool.board_live_grid the live channel grid follows the real SBUS input: the rx stick moved by the bench controller shows the exact SBUS count on its channel, then its rest value again', async ({ page, device, hilCtx }) => {
  const a = hilCtx.args;
  const cell = page.locator(`#channels-grid .ch-row[data-ch="${a.ch}"] .ch-val`);
  await T.openTool(page);
  await connectPiped(page);
  try {
    await expect(cell, 'the stick at rest').toHaveText(String(a.rest), { timeout: 10_000 });
    for (const p of a.points) {
      await sbus({ t: 'a', lx: 0, ly: 0, rx: p.arg, ry: 0 });
      await expect(cell, `CH${a.ch} with the stick at its ${p.value} count`).toHaveText(String(p.value), { timeout: 5_000 });
    }
  } finally {
    await sbus({ t: 'a', lx: 0, ly: 0, rx: 0, ry: 0 });
  }
  await expect(cell, 'the stick centred again').toHaveText(String(a.rest), { timeout: 5_000 });
  await disconnectPiped(page, device);
  expect(device.sentTypes.filter((t) => t === 'SET_CONFIG' || t === 'TEST_ACTION' || t === 'TRIGGER')).toEqual([]);
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});

// nct.clips.list on the board, read-only: openClipsModal -> ?REC,LS (index.html:6164-6191), rendered by renderClips
// (:6298-6349). The harness read the same list over its own ?REC,LS; Greg's clips are only listed, never touched.
test("nctool.board_clips_list the Clips panel lists the real NaviCore's clips as ?REC,LS names them, in order, with the storage bar when the clips partition reports one; only ?REC,LS is sent", async ({ page, device, hilCtx }) => {
  const { names, storage } = hilCtx.args;
  await T.openTool(page);
  await connectPiped(page);
  await page.locator('#btn-clips').click();
  await expect(page.locator('#clips-modal')).toHaveClass(/open/);
  if (names.length) {
    await expect.poll(() => page.locator('#clips-list > div > div:first-child > div:first-child').allTextContents(),
                      { timeout: 15_000 }).toEqual(names);
  } else {
    await expect(page.locator('#clips-empty')).toHaveText('No saved clips. Record one above, then Refresh.', { timeout: 15_000 });
  }
  await expect(page.locator('#clips-storage')).toBeVisible({ visible: storage });
  await page.evaluate(() => closeClipsModal());
  await disconnectPiped(page, device);
  // '?REC' is the connect's identify probe (D-NC70: the first line on a fresh port), '?REC,LS' the panel's only verb
  expect([...new Set(device.sentCli)], 'CLI verbs the tool sent').toEqual(['?REC', '?REC,LS']);
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});

// nct.cmdlib.custom_sync on the board, read-only: "Load from NaviCore" (loadCmdlibFromBoard, index.html:14500-14505)
// sends GET_CMDLIB; the CMDLIB handler (:9159-9169) merges the droid's boards into the page's catalog and its own
// localStorage, and records the droid's signature. Nothing goes back to the board (no SET_CMDLIB, no bulk upload).
test("nctool.board_cmdlib_load \"Load from NaviCore\" pulls the real board's stored command library over USB: every board id it holds is merged, the droid's FNV-1a is recorded as the synced signature, and nothing is written back", async ({ page, device, hilCtx }) => {
  const { ids, hash, mode, btn } = hilCtx.args;
  await T.openTool(page);
  await connectPiped(page);
  await page.evaluate(({ mode, btn }) => { setEditingMode(mode); openModal(btn); openCmdLibrary('fields-1-0'); }, { mode, btn });
  await expect(page.locator('#cmdlib-modal')).toHaveClass(/open/);
  await expect(page.locator('#cmdlib-load-board')).toBeEnabled();
  await page.locator('#cmdlib-load-board').click();
  await expect.poll(() => T.termLines(page, /\[cmdlib\] loaded \d+ custom board/), { timeout: 30_000 }).toHaveLength(1);
  const [line] = await T.termLines(page, /\[cmdlib\] loaded \d+ custom board/);
  expect(+/loaded (\d+) custom/.exec(line)[1], 'boards merged').toBe(ids.length);
  expect(await page.evaluate(() => localStorage.getItem('nc_cmdlib_droid_sig'))).toBe(String(hash));
  if (ids.length) expect(await page.evaluate(() => [..._cmdlibCustomIds()].sort())).toEqual([...ids].sort());
  await page.evaluate(() => { closeCmdLibrary(); closeModal(); });
  expect(await page.evaluate(() => _configUnsaved())).toBe(false);
  await disconnectPiped(page, device);
  expect(sent(device, 'GET_CMDLIB')).toBe(1);
  expect(device.sentTypes.filter((t) => t === 'SET_CMDLIB' || t === 'SET_CONFIG')).toEqual([]);
  expect(device.sentLog.filter((l) => l.how === 'text'), 'bulk envelopes or other bare text').toEqual([]);
  expect(device.errors, 'bridge errors').toEqual([]);
  T.expectNoPageErrors(page);
});

// nct.fw.ota_usb on the board (opt-in navicore_ota_full): "Update over USB (OTA)" (otaUpdateOverUsb,
// index.html:17412-17607) with GitHub's firmware/ listing served by page.route as the bench image NaviCore already
// runs, app only (no bootloader/table pair: fetchFirmwareImages flashes app only, flasher.js:185-220, and OTA takes
// only the 0x10000 image). The windowed ?OTALOCAL stream goes through the pipe; after [OTA:END,OK] NaviCore restarts
// into the other slot and the tool reconnects by itself (reopenAfterFlash, :17291-17359). The harness checks the slot
// and the App SHA256 afterwards and puts the boot slot back with hil/ncflash.
test('nctool.board_ota_usb_same_image the tool\'s Update over USB streams the bench image to the real NaviCore through the pipe; NaviCore verifies it and restarts, and the tool reconnects to it by itself, on the same version with nothing to save', async ({ page, device, hilCtx }) => {
  test.setTimeout(420_000);
  const { image, version } = hilCtx.args;
  const app = fs.readFileSync(image);
  const set = { version, app, names: { app: `NaviCore_${version}_ESP32S3.bin`, part: `NaviCore_${version}_ESP32S3_part.bin`,
                                       boot: F.BOOT_NAME } };
  const gh = await F.mockFirmware(page, set, { omit: ['boot', 'part'], decoys: false });
  await T.openTool(page);
  await connectPiped(page);
  await page.locator('#btn-hwsetup').click();
  await page.locator('#cfg-tabs .cfg-tab[data-tab="firmware"]').click();
  await expect(page.locator('#fw-latest-version')).toHaveText(version, { timeout: 15_000 });
  await expect(page.locator('#fw-current-version')).toHaveText(version);
  await page.locator('#btn-fw-ota').click();
  // The three ends otaUpdateOverUsb can reach (index.html:17599-17619); 'OTA complete ✓ — waiting ...' is not one.
  const END = /^(OTA complete — reconnected\. ✓|OTA complete ✓ — board updated; reconnect when it is back|OTA failed: .*)$/;
  await expect(page.locator('#fw-status')).toHaveText(END, { timeout: 360_000 });
  await expect(page.locator('#fw-status')).toHaveText('OTA complete — reconnected. ✓');
  expect(gh.fetched, 'files downloaded').toEqual([set.names.app]);
  await expect(page.locator('#fw-current-version')).toHaveText(version);
  const s = await T.state(page);
  expect([s.connected, s.configLoaded, s.pendingSave]).toEqual([true, true, false]);
  expect(await page.evaluate(() => _configUnsaved()), 'unsaved edits after the reconnect').toBe(false);
  await disconnectPiped(page, device);
  expect(device.sentTypes.filter((t) => t === 'SET_CONFIG' || t === 'RESET_DEFAULTS')).toEqual([]);
  // The restart re-enumerates NaviCore's USB under the harness's handle; a write the page made in that gap may have
  // failed at the bridge. Noted, not failed: the verdict is the tool's own status line and the harness's STATUS.
  if (device.errors.length) await hil.note(`board_ota_usb_same_image: ${device.errors.length} bridge write(s) failed across the restart`);
  T.expectNoPageErrors(page);
});
