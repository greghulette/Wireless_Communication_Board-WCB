// Loading a config and saving it back: applyConfig, the branch diff, the ACK correlation, Reset to Defaults, Refresh
// and the Config window's close prompt (docs/hil_plan/NAVICORE.md §2, nct.config.*, nct.save.*, L1). No board: the
// emulator stores a SET_CONFIG the way rcConfigFromJSON does, so "what the board holds afterwards" is checkable.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

// Paths where two JSON values differ (for readable failures).
function diffPaths(a, b, p = '', out = []) {
  if (JSON.stringify(a) === JSON.stringify(b)) return out;
  if (a && b && typeof a === 'object' && typeof b === 'object' && Array.isArray(a) === Array.isArray(b)) {
    for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) diffPaths(a[k], b[k], `${p}.${k}`, out);
  } else out.push(`${p}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);
  return out;
}
const stable = (v) => JSON.stringify(v, (k, x) => (x && typeof x === 'object' && !Array.isArray(x)
  ? Object.fromEntries(Object.keys(x).sort().map((key) => [key, x[key]])) : x));

async function openConfigTab(page, tab) {
  if (!(await page.locator('#hwsetup-modal.open').count())) await page.locator('#btn-hwsetup').click();
  await page.locator(`#cfg-tabs .cfg-tab[data-tab="${tab}"]`).click();
}
async function fill(page, sel, value) {
  await page.locator(sel).fill(String(value));
  await page.locator(sel).dispatchEvent('change');
}

test.describe(() => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.apply_all_keys every key GET_CONFIG carries lands in the tool unchanged — but for four documented normalizations — and the General and WCB fields show it', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    const live = await page.evaluate(() => JSON.parse(JSON.stringify(config)));
    const expected = clone(BENCH);
    // applyConfig's own normalizations, each done before the baseline snapshot:
    expected.maestros.forEach((m) => { if (!m.channels) m.channels = []; });      // padded pre-baseline (index.html:9713-9719)
    expected.serialLabels = {};                                                   // absent = old firmware -> {} (:9756)
    // The audio destinations MERGE over the tool's defaults ({...config.mp3Dest, ...data.mp3Dest}, :9739-9744), so the
    // key the board leaves out for its transport comes from the default: an 'off'/'serial' dest keeps target '2'.
    for (const k of ['mp3Dest', 'dfpDest']) expected[k] = { transport: 'off', target: '2', port: 'S3', ...expected[k] };
    expect(diffPaths(JSON.parse(stable(live)), JSON.parse(stable(expected))), 'config after load vs the board').toEqual([]);

    const ui = await page.evaluate(() => Object.fromEntries(['tapwin-input', 'holdms-input', 'switchsettle-input', 'chrate-input',
      'board-select', 'matrix-debounce-input', 'bind-mode', 'cfg-matrix-ch', 'wcb-oct2', 'wcb-oct3', 'wcb-quantity',
      'wcb-deviceid', 'wcb-channel', 'modereport-wcb', 'modereport-template', 'stats-wcb']
      .map((id) => [id, document.getElementById(id).value])));
    expect(ui).toEqual({
      'tapwin-input': String(BENCH.tapWindowMs), 'holdms-input': String(BENCH.holdMs), 'switchsettle-input': String(BENCH.switchSettleMs),
      'chrate-input': String(BENCH.chRateHz), 'board-select': String(BENCH.boardType), 'matrix-debounce-input': String(BENCH.matrixDebounceFrames),
      'bind-mode': String(BENCH.funcBindings.mode), 'cfg-matrix-ch': String(BENCH.matrixChannel),
      'wcb-oct2': BENCH.wcbNetwork.macOct2.toString(16).toUpperCase().padStart(2, '0'),
      'wcb-oct3': BENCH.wcbNetwork.macOct3.toString(16).toUpperCase().padStart(2, '0'),
      'wcb-quantity': String(BENCH.wcbNetwork.quantity), 'wcb-deviceid': String(BENCH.wcbNetwork.deviceId),
      'wcb-channel': String(BENCH.wcbNetwork.channel), 'modereport-wcb': String(BENCH.modeReport.wcb),
      'modereport-template': BENCH.modeReport.template, 'stats-wcb': String(BENCH.statsReport.wcb),
    });
    const boxes = await page.evaluate(() => ['sbus-out-toggle', 'modereport-enabled', 'stats-enabled', 'bcout-S3', 'bcin-S4']
      .map((id) => document.getElementById(id).checked));
    expect(boxes).toEqual([BENCH.sbusOutEnabled, BENCH.modeReport.enabled, BENCH.statsReport.enabled,
                           BENCH.serialBcast.S3.out, BENCH.serialBcast.S4.in]);
    T.expectNoPageErrors(page);
  });

  test('nctool.no_phantom_diff opening every Config tab and every editor, then Save, sends nothing; the last tab is remembered and a stale name falls back to channels', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    for (const tab of ['general', 'channels', 'logical', 'transmitter', 'wcb', 'audio', 'maestro', 'wled', 'smoothing', 'serial', 'firmware']) {
      await openConfigTab(page, tab);
      await expect(page.locator(`#cfg-pane-${tab}`)).toHaveClass(/active/);
    }
    await page.evaluate(() => closeHwSetup());
    // Every editor, opened and cancelled: a mapped button, a switch with actions, a knob with outputs.
    await page.evaluate(() => { openModal(1); closeModal(); openSwitchModal('SC'); closeModal(); openKnobModal('J2'); closeModal(); });
    await page.locator('#btn-hwsetup').click();
    await expect(page.locator('#push-budget')).toHaveText('No changes to save');
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No changes to save'));
    expect(emu.requests('SET_CONFIG')).toEqual([]);
    expect(await page.evaluate(() => _configUnsaved())).toBe(false);
    await page.evaluate(() => closeHwSetup());
    // rcConfigLastTab: a pre-merge 'mp3' opens the Audio pane; a name that no longer exists falls back to channels.
    await page.evaluate(() => localStorage.setItem('rcConfigLastTab', 'mp3'));
    await page.locator('#btn-hwsetup').click();
    await expect(page.locator('#cfg-pane-audio')).toHaveClass(/active/);
    await page.evaluate(() => { closeHwSetup(); localStorage.setItem('rcConfigLastTab', 'bogus'); });
    await page.locator('#btn-hwsetup').click();
    await expect(page.locator('#cfg-pane-channels')).toHaveClass(/active/);
    T.expectNoPageErrors(page);
  });

  test('nctool.save_diff_payload an edit typed into General is the only thing Save sends (sys:1, saveId 1); the hold clamp matches the firmware so the board echoes what was sent; a second Save sends nothing', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openConfigTab(page, 'general');
    await fill(page, '#tapwin-input', 600);
    await fill(page, '#holdms-input', 620);                     // below tapWindow + 100: the tool clamps as rcConfigFromJSON does
    await page.locator('#btn-hwsetup-save').click();
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(sent).toEqual({ sys: 1, type: 'SET_CONFIG', data: { tapWindowMs: 600, holdMs: 850 }, saveId: await page.evaluate(() => _saveWireId(1)) });
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Config saved to NaviCore'));
    expect(await page.evaluate(() => [_configBaseline.tapWindowMs, _configBaseline.holdMs, !!_pendingSaveBaseline])).toEqual([600, 850, false]);
    expect([emu.config.toJSON().tapWindowMs, emu.config.toJSON().holdMs], 'the board holds exactly what was sent').toEqual([600, 850]);
    // Refresh: the board's own copy comes back and is already the baseline — no phantom diff after a save.
    await page.evaluate(() => closeHwSetup());
    await page.locator('#btn-refresh').click();
    await T.waitRequests(emu, 'GET_CONFIG', 2);
    await page.waitForTimeout(300);
    await page.locator('#btn-hwsetup').click();
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No changes to save'));
    expect(emu.requests('SET_CONFIG')).toHaveLength(1);
    T.expectNoPageErrors(page);
  });

  test('nctool.save_ack_correlation a NACK and a 12 s silence leave the baseline alone, an overlapping Save is refused, a stale saveId is ignored, an ACK with no saveId (old firmware) still counts', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    const setSbus = (on) => page.evaluate((on) => { config.sbusOutEnabled = on; return saveConfigToBoard(); }, on);
    const base = () => page.evaluate(() => _configBaseline.sbusOutEnabled);
    // 1. NACK: the rejected edit stays out of the baseline, so the next Save sends it again.
    emu.override.SET_CONFIG = (m) => `{"type":"ACK","of":"SET_CONFIG","ok":false,"msg":"config apply failed","saveId":${m.saveId}}`;
    await setSbus(false);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Save rejected by NaviCore'));
    expect(await base()).toBe(true);
    delete emu.override.SET_CONFIG;
    // 2. No answer for 12 s: the watchdog frees the lock, the baseline stays.
    emu.hold.add('SET_CONFIG');
    await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
    await setSbus(false);
    // 3. A Save while one is in flight is refused, not sent.
    await page.evaluate(() => saveConfigToBoard());
    expect(await T.toasts(page)).toContainEqual(expect.stringContaining('A save is still in progress'));
    expect(emu.requests('SET_CONFIG')).toHaveLength(2);
    await page.clock.runFor(12_500);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No response from NaviCore'));
    expect(await page.evaluate(() => [!!_pendingSaveBaseline, _configBaseline.sbusOutEnabled])).toEqual([false, true]);
    // 4. The next save (saveId 3) is in flight when the timed-out one's ACK (saveId 2) finally lands: ignored.
    await setSbus(false);
    const ids = emu.requests('SET_CONFIG').map((m) => m.saveId);
    expect(ids).toEqual(await page.evaluate(() => [1, 2, 3].map(_saveWireId)));   // a per-tab base plus the count (D-NC35)
    emu.held.shift().fn();                                        // the stale ACK, saveId 2
    await page.waitForTimeout(300);
    expect(await page.evaluate(() => [!!_pendingSaveBaseline, _configBaseline.sbusOutEnabled]), 'a stale ACK must not advance the baseline').toEqual([true, true]);
    emu.held.shift().fn();                                        // its own ACK, saveId 3
    await expect.poll(() => page.evaluate(() => [!!_pendingSaveBaseline, _configBaseline.sbusOutEnabled])).toEqual([false, false]);
    emu.hold.delete('SET_CONFIG');
    // 5. Firmware that echoes no saveId: the ACK still counts (the old, uncorrelated behaviour).
    emu.override.SET_CONFIG = () => '{"type":"ACK","of":"SET_CONFIG","ok":true}';
    await setSbus(true);
    await expect.poll(() => page.evaluate(() => [!!_pendingSaveBaseline, _configBaseline.sbusOutEnabled])).toEqual([false, true]);
    T.expectNoPageErrors(page);
  });

  test('nctool.reset_defaults_flow Restore Defaults asks, sends RESET_DEFAULTS then GET_CONFIG, and reports success only once the fresh CONFIG is in; a missing CONFIG warns after 12 s; bridged it refuses', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [false, true, true]);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.locator('#btn-hwsetup').click();
    await page.locator('#btn-defaults').click();                  // dismissed
    expect(dialogs[0].message).toBe('Reset all button mappings to factory defaults?');
    expect(emu.requests('RESET_DEFAULTS')).toEqual([]);
    emu.hold.add('GET_CONFIG');
    await page.locator('#btn-defaults').click();                  // accepted
    await T.waitRequests(emu, 'RESET_DEFAULTS');
    await T.waitRequests(emu, 'GET_CONFIG', 2);
    const order = emu.rx.filter((r) => r.json && /RESET_DEFAULTS|GET_CONFIG/.test(r.json.type)).map((r) => r.json.type);
    expect(order.slice(-2)).toEqual(['RESET_DEFAULTS', 'GET_CONFIG']);
    expect(await T.toasts(page)).not.toContainEqual(expect.stringContaining('Reset to factory defaults'));
    emu.release('GET_CONFIG');
    await expect.poll(() => T.toasts(page)).toContainEqual('✓Reset to factory defaults');
    expect(await page.evaluate(() => Object.keys(config.mappings).length), 'the defaults (no mappings) are loaded').toBe(0);
    // No CONFIG after a reset: the watchdog says so instead of claiming success.
    emu.hold.add('GET_CONFIG');
    await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
    await page.locator('#btn-defaults').click();
    await T.waitRequests(emu, 'RESET_DEFAULTS', 2);
    await page.clock.runFor(400);                                 // the 300 ms gap before the re-read
    await T.waitRequests(emu, 'GET_CONFIG', 3);
    await page.clock.runFor(12_500);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No response after reset'));
    // Over the bridge the tool refuses outright (its comment says the mesh has no RESET_DEFAULTS; the firmware now
    // does handle one, rc_telemetry.h:2451 — D-NC16's stale comment).
    const before = emu.rx.length;
    await page.evaluate(() => onViaWcbToggle(true));
    await page.locator('#btn-defaults').click();
    expect(dialogs.at(-1)).toMatchObject({ type: 'alert' });
    expect(dialogs.at(-1).message).toContain('Restore Defaults needs a direct USB connection');
    expect(emu.rx.slice(before).filter((r) => /RESET_DEFAULTS/.test(r.line))).toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.refresh_overwrites_edits (should) Refresh with unsaved edits asks first; declined, the edit stays and nothing is re-read (D-NC31)', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [false]);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openConfigTab(page, 'serial');
    await page.locator('#slabel-S3').fill('HILUNSAVED');          // writes config.serialLabels.S3 as you type
    expect(await page.evaluate(() => _configUnsaved())).toBe(true);
    await page.evaluate(() => closeHwSetup());
    dialogs.length = 0;
    await page.locator('#btn-refresh').click();
    await page.waitForTimeout(500);
    expect(dialogs.map((d) => d.type), 'a prompt before discarding unsaved edits').toEqual(['confirm']);
    expect(emu.requests('GET_CONFIG')).toHaveLength(1);
    expect(await page.evaluate(() => config.serialLabels.S3)).toBe('HILUNSAVED');
    T.expectNoPageErrors(page);
  });

  test('nctool.close_prompt closing Config prompts only for edits made in that window and not yet on the board: Cancel keeps them unsaved, OK saves', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [false, true]);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.locator('#btn-hwsetup').click();
    await page.evaluate(() => closeHwSetup());
    expect(dialogs).toEqual([]);                                  // nothing changed: no prompt
    await openConfigTab(page, 'general');
    await page.locator('#sbus-out-toggle').click();               // writes config.sbusOutEnabled
    await page.evaluate(() => closeHwSetup());                    // prompt -> Cancel
    expect(dialogs[0].message).toContain('You have unsaved config changes');
    await expect(page.locator('#hwsetup-modal')).not.toHaveClass(/open/);
    expect(emu.requests('SET_CONFIG')).toEqual([]);
    expect(await page.evaluate(() => [config.sbusOutEnabled, _configUnsaved()])).toEqual([!BENCH.sbusOutEnabled, true]);
    // Reopened and closed untouched: the earlier edit is still unsaved, but it was not made in THIS window.
    await page.locator('#btn-hwsetup').click();
    await page.evaluate(() => closeHwSetup());
    expect(dialogs).toHaveLength(1);
    // An edit in this window, OK at the prompt: it saves (with the earlier edit, which is part of the same diff).
    await openConfigTab(page, 'general');
    await fill(page, '#stats-wcb', 3);
    await page.evaluate(() => closeHwSetup());
    expect(dialogs).toHaveLength(2);
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(sent.data).toEqual({ sbusOutEnabled: !BENCH.sbusOutEnabled, statsReport: { enabled: BENCH.statsReport.enabled, wcb: 3 } });
    await expect.poll(() => page.evaluate(() => _configUnsaved())).toBe(false);
    await page.locator('#btn-hwsetup').click();
    await page.evaluate(() => closeHwSetup());
    expect(dialogs).toHaveLength(2);
    T.expectNoPageErrors(page);
  });
});
