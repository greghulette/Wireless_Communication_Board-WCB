// The action editors (button / switch / knob modals), the per-action Test, the command view's limits, and the WCB
// Network pane with its credential profiles (docs/hil_plan/NAVICORE.md §2, nct.edit.*, L1). No board: the emulator
// stores what a Save sends the way rcConfigFromJSON does — ArduinoJson 7's type-strict `|` included — so a value the
// board would silently drop is visible after a Refresh.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

// "branch.path: before -> after" for every leaf that differs.
function changes(before, after, p = '', out = []) {
  if (JSON.stringify(before) === JSON.stringify(after)) return out;
  if (before && after && typeof before === 'object' && typeof after === 'object') {
    for (const k of new Set([...Object.keys(before), ...Object.keys(after)])) changes(before[k], after[k], p ? `${p}.${k}` : k, out);
  } else out.push(`${p}: ${JSON.stringify(before)} -> ${JSON.stringify(after)}`);
  return out;
}
const modalApply = (page) => page.locator('#modal .modal-footer .btn-success').click();

test.describe('the bench config', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.noop_apply_every_editor (should) opening every button, switch and knob editor and applying it unchanged changes nothing, so Save sends nothing (D-NC32)', async ({ page, emu }) => {
    test.fail(true, 'known tool defect D-NC32: read-back rewrites skipRunning true as 1 (readActionFromFid, index.html:15425-15459) ' +
                    'and saveKnobModal adds smoothProfile/easeSwitchOverride/midClosed/releaseIdleMs defaults (:16127-16170)');
    await T.openTool(page);
    await T.connectUsb(page, emu);
    const diff = await page.evaluate(() => {
      const before = JSON.parse(JSON.stringify(_configBaseline));
      for (const key of Object.keys(config.mappings)) { setEditingMode(Math.floor(+key / 100)); openModal(+key % 100); saveButton(); }
      setEditingMode(1);
      for (const sw of Object.keys(config.switches)) { openSwitchModal(sw); saveSwitchModal(); }
      for (const kn of Object.keys(config.knobs)) { openKnobModal(kn); saveKnobModal(); }
      return { before, after: JSON.parse(JSON.stringify(config)) };
    });
    const changed = changes(diff.before, diff.after).filter((c) => !c.startsWith('maestros') && !/^(mp3Dest|dfpDest|serialLabels)/.test(c));
    expect(changed, 'what a no-op Apply rewrote').toEqual([]);
    await page.evaluate(() => saveConfigToBoard());
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No changes to save'));
    expect(emu.requests('SET_CONFIG')).toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.skip_running_saved (should) Apply on a mapping whose action has "skip if running" keeps the gate on the board (the tool sends skipRunning:1, which ArduinoJson 7 reads as false)', async ({ page, emu }) => {
    test.fail(true, 'known tool defect: readActionFromFid writes skipRunning: 1 (index.html:15433, :15446, :15458; _cmdlibUse :14166); rcConfigFromJSON reads it with ' +
                    '`obj["skipRunning"] | false`, and ArduinoJson 7.4.3 returns the default unless the value IS a boolean ' +
                    '(VariantOperators.hpp:34-40, ConverterImpl.hpp:124-127) — the hidden checkbox exists only so a saved value round-trips');
    await T.openTool(page);
    await T.connectUsb(page, emu);
    expect(BENCH.mappings['102'].t1[0].skipRunning).toBe(true);
    await page.locator('#assignment-list > div').nth(1).click();   // button 2, mode 1: the mapping with the gate
    await expect(page.locator('#modal')).toHaveClass(/open/);
    await modalApply(page);
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(Object.keys(sent.data.mappings)).toEqual(['102']);
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    await page.locator('#btn-refresh').click();
    await T.waitRequests(emu, 'GET_CONFIG', 2);
    await page.waitForTimeout(300);
    expect(emu.config.toJSON().mappings['102'].t1[0].skipRunning, 'the board after the Save').toBe(true);
    expect(await page.evaluate(() => config.mappings['102'].t1[0].skipRunning), 'the tool after a Refresh').toBe(true);
    T.expectNoPageErrors(page);
  });

  test('nctool.button_modal_trigger Shift-click fires TRIGGER (Ctrl = double, Alt = triple, Ctrl+Alt = long press); a new mapping built in the modal saves as exactly that mapping, and Clear All sends {}', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    const zone = page.locator('g.btn-zone[data-btn="3"]:visible').first();
    await zone.click({ modifiers: ['Shift'] });
    await zone.click({ modifiers: ['Shift', 'Control'] });
    await zone.click({ modifiers: ['Shift', 'Alt'] });
    await zone.click({ modifiers: ['Shift', 'Control', 'Alt'] });
    const trig = (await T.waitRequests(emu, 'TRIGGER', 4)).map((m) => [m.mode, m.btn, m.tap]);
    expect(trig).toEqual([[1, 3, 1], [1, 3, 2], [1, 3, 3], [1, 3, 4]]);
    await expect(page.locator('#modal')).not.toHaveClass(/open/);  // a Shift-click never opens the editor
    // Mode 2, button 2 has no mapping on the bench: build one.
    await page.evaluate(() => setEditingMode(2));
    await page.locator('#assignment-list > div').nth(1).click();
    await expect(page.locator('#modal-title')).toHaveText(/^Button 2:/);
    await page.locator('#fields-1-0-cmd').fill(';S2HILNEW');
    await page.locator('#note-1-0').fill('hil note');
    await page.locator('#tier-note-1').fill('HILNAME');
    await page.locator('#tier-card-1 .btn-add-action').click();
    // The tab badge counts what a Save would keep: the new blank row is not counted.
    await expect(page.locator('#tier-tab-count-1')).toHaveText('1');
    await page.locator('#fields-1-1-cmd').fill(';S3HILNEW2');
    await page.locator('#delay-1-1').fill('250');
    await modalApply(page);
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(sent.data).toEqual({ mappings: { 202: { exclusive: false, t1note: 'HILNAME', t1: [
      { type: 'wcb_unicast', target: '1', cmd: ';S2HILNEW', note: 'hil note' },
      { type: 'wcb_unicast', target: '1', cmd: ';S3HILNEW2', delay: 250 }] } } });
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().mappings['202']).toEqual({ exclusive: false, t1: sent.data.mappings['202'].t1, t1note: 'HILNAME' });
    // Clear All removes it; the Save ships {} so the firmware memsets the slot.
    await page.locator('#assignment-list > div').nth(1).click();
    await page.locator('#modal .modal-footer .btn-ghost', { hasText: 'Clear All' }).click();
    await page.evaluate(() => saveConfigToBoard());
    const all = await T.waitRequests(emu, 'SET_CONFIG', 2);
    expect(all[1].data).toEqual({ mappings: { 202: {} } });
    await expect.poll(() => emu.config.toJSON().mappings['202']).toBeUndefined();
    T.expectNoPageErrors(page);
  });

  test("nctool.test_action_button a row's ▶ Test fires TEST_ACTION with the row's current values minus delay and note; a recorder control is refused locally", async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.locator('#assignment-list > div').nth(9).click();   // button 10: three actions, one delayed, one noted
    await page.locator('#fields-1-0-cmd').fill(';S2HILTESTED');     // an unsaved edit is what gets fired
    await page.locator('#action-1-0 .btn-test-action').click();
    await page.locator('#action-1-1 .btn-test-action').click();
    await page.locator('#action-1-2 .btn-test-action').click();
    const acts = (await T.waitRequests(emu, 'TEST_ACTION', 3)).map((m) => m.action);
    expect(acts).toEqual([
      { type: 'wcb_unicast', target: '1', cmd: ';S2HILTESTED' },
      { type: 'wcb_unicast', target: '2', cmd: ';S2HIL110B' },              // delay 500 dropped: fired now
      { type: 'wcb_broadcast', cmd: ';W1;S3HIL110C' },                        // note dropped: one packet bridged
    ]);
    expect(emu.requests('SET_CONFIG'), 'Test never saves').toEqual([]);
    await page.evaluate(() => closeModal());
    await page.locator('#assignment-list > div').nth(6).click();    // button 7: t1 is "play intro"
    await page.locator('#action-1-0 .btn-test-action').click();
    await expect.poll(() => T.termLines(page, /can't be test-fired/)).toHaveLength(1);
    expect(emu.requests('TEST_ACTION')).toHaveLength(3);
    T.expectNoPageErrors(page);
  });

  test('nctool.test_action_refusal_shown (should) a TEST_ACTION the board refuses (ok:false) is reported to the user, not only echoed raw in the closed terminal (D-NC20)', async ({ page, emu }) => {
    test.fail(true, 'known tool gap D-NC20: the ACK handler acts only on SET_CONFIG ACKs (index.html:9591-9627); a TEST_ACTION ' +
                    'refusal reaches only the raw terminal echo');
    emu.override.TEST_ACTION = () => '{"type":"ACK","of":"TEST_ACTION","ok":false,"msg":"destination disabled"}';
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.locator('#assignment-list > div').nth(0).click();
    await page.locator('#action-1-0 .btn-test-action').click();
    await T.waitRequests(emu, 'TEST_ACTION');
    await page.waitForTimeout(500);
    expect((await T.toasts(page)).filter((t) => /test/i.test(t) && /fail|refus|not|✗|⚠/i.test(t)), 'a visible refusal').not.toEqual([]);
    T.expectNoPageErrors(page);
  });

  test('nctool.command_view_limits the command field reserves room for the ;W<n>;S<p> prefix (95 stored), warns past it, and flags a chained unicast of implicitly-routed verbs only', async ({ page, emu }) => {
    emu.roster = { known: [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1], online: [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1] };   // WCB 12 discovered
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.clock.fastForward(3100);                            // the 3 s status poll learns the roster
    await page.waitForFunction(() => Array.isArray(wcbKnown) && !!wcbKnown[11]);
    // SI position 0 is ";W1;S3HILSIA": shown bare, its WCB and port in the two dropdowns.
    await page.evaluate(() => openSwitchModal('SI'));
    const cmd = page.locator('#fields-p0-0-cmd');
    await expect(cmd).toHaveValue('HILSIA');
    expect(await page.evaluate(() => [document.getElementById('fields-p0-0-wcbsel').value, document.getElementById('fields-p0-0-portsel').value])).toEqual(['1', '3']);
    await page.locator('#fields-p0-0-wcbsel').selectOption('12');
    await page.locator('#fields-p0-0-portsel').selectOption('3');
    expect(await cmd.evaluate((e) => e.maxLength), 'a 7-character prefix reserves 7').toBe(95 - ';W12;S3'.length);
    const warn = page.locator('#fields-p0-0 > div').filter({ hasText: 'Too long by' });
    await cmd.evaluate((e) => { e.value = 'x'.repeat(90); e.dispatchEvent(new Event('input')); });
    await expect(warn).toHaveText(/Too long by 2 characters/);
    await page.evaluate(() => closeModal());
    // Button 13 (;S2HIL113^;S3HIL113 to WCB 1): ;s never routes, so no chain warning; ;M does.
    await page.locator('#assignment-list > div').nth(12).click();
    const chain = page.locator('#fields-1-0 > div').filter({ hasText: 'Chained command sent to one board' });
    await expect(chain).toBeHidden();
    const setCmd = (v) => page.locator('#fields-1-0-cmd').evaluate((e, v) => { e.value = v; e.dispatchEvent(new Event('input')); }, v);
    await setCmd(';M11^;M12');
    await expect(chain).toBeVisible();
    await setCmd(';w3;s4:PP100^;w3;s4:PL5');                        // explicit routing is never capped
    await expect(chain).toBeHidden();
    await setCmd(';M11^;M12');
    await page.locator('#fields-1-0-wcbsel').selectOption('0');      // broadcast: no one-hop cap either
    await expect(chain).toBeHidden();
    T.expectNoPageErrors(page);
  });

  test('nctool.command_view_cap_on_open (should) an action stored with a ;W<n>;S<p> prefix opens with the prefix already reserved in the field cap', async ({ page, emu }) => {
    test.fail(true, 'known tool defect: _appendCommandView runs sync() and refreshLenWarn() while the row is still detached, so ' +
                    'document.getElementById(fid + "-destsel") is null and the cap is set to the full 95 (index.html:15108-15123, reached from sync() at :15192 and the render-time call at :15235); ' +
                    'the first keystroke re-applies it, and the render-time "already over-length" flag never fires');
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.evaluate(() => openSwitchModal('SI'));
    const cmd = page.locator('#fields-p0-0-cmd');
    await expect(cmd).toHaveValue('HILSIA');
    expect(await cmd.evaluate((e) => e.maxLength), 'the cap on open for ";W1;S3" + command').toBe(95 - ';W1;S3'.length);
    T.expectNoPageErrors(page);
  });

  test('nctool.wcb_network_profiles the dirty badge tracks the WCB Network fields, a USB Save carries the branch, a profile loads into the fields, and a seventh profile is refused', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, ['HIL3', 'HIL4', 'HIL5', 'HIL6', 'HIL7']);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await page.locator('#btn-hwsetup').click();
    await page.locator('#cfg-tabs .cfg-tab[data-tab="wcb"]').click();
    const badge = page.locator('#wcb-network-dirty');
    await expect(badge).toBeHidden();
    await page.locator('#wcb-quantity').fill('3');
    await expect(badge).toBeVisible();
    await page.locator('#wcb-quantity').fill(String(BENCH.wcbNetwork.quantity));
    await expect(badge).toBeHidden();
    await page.locator('#wcb-quantity').fill('3');
    await page.locator('#btn-hwsetup-save').click();
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(Object.keys(sent.data)).toEqual(['wcbNetwork']);
    expect(sent.data.wcbNetwork.quantity).toBe(3);
    await expect.poll(() => emu.config.toJSON().wcbNetwork.quantity).toBe(3);
    // Loading a profile writes its identity into the fields (Save over USB applies it).
    await page.locator('#wcb-profile-list input[type=radio]').nth(1).click();
    expect(await page.evaluate(() => ({ ...config.wcbNetwork, password: undefined }))).toEqual({ ...BENCH.wcbProfiles[1], name: undefined, password: undefined });
    await expect(page.locator('#wcb-channel')).toHaveValue(String(BENCH.wcbProfiles[1].channel));
    // Four more profiles fill the six slots; a seventh is refused and nothing is replaced.
    const add = page.locator('button', { hasText: 'Save current credentials as profile' });
    for (let i = 0; i < 5; i++) await add.click();
    expect(dialogs.filter((d) => d.type === 'prompt')).toHaveLength(5);
    expect(await page.evaluate(() => config.wcbProfiles.map((p) => p.name))).toEqual(['HILdev', 'HILdroid', 'HIL3', 'HIL4', 'HIL5', 'HIL6']);
    expect(await T.toasts(page)).toContainEqual(expect.stringContaining('Profile limit reached (6)'));
    T.expectNoPageErrors(page);
  });
});

test.describe('the bench config, through a WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb' } });

  test('nctool.wcb_network_bridged_strip over the bridge a WCB Network edit is not sent: alone it says so and sends nothing, with other edits it is stripped and kept out of the new baseline', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectViaWcb(page);
    await page.locator('#btn-hwsetup').click();
    await page.locator('#cfg-tabs .cfg-tab[data-tab="wcb"]').click();
    await page.locator('#wcb-quantity').fill('3');
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('WCB Network changes need a Direct USB connection'));
    expect(emu.rx.filter((r) => /SET_CONFIG/.test(r.line))).toEqual([]);
    // With another edit: that one goes, the WCB Network fields do not, and the baseline keeps the board's values so
    // a later USB Save still has the edit to send.
    await page.locator('#cfg-tabs .cfg-tab[data-tab="general"]').click();
    await page.locator('#sbus-out-toggle').click();
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('WCB Network changes skipped'));
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    const sent = emu.rx.filter((r) => /SET_CONFIG/.test(r.line)).map((r) => r.json);
    expect(sent).toHaveLength(1);
    expect(sent[0].data).toEqual({ sbusOutEnabled: !BENCH.sbusOutEnabled });
    expect(await page.evaluate(() => [_configBaseline.wcbNetwork.quantity, config.wcbNetwork.quantity])).toEqual([BENCH.wcbNetwork.quantity, 3]);
    expect(emu.config.toJSON().wcbNetwork.quantity).toBe(BENCH.wcbNetwork.quantity);
    T.expectNoPageErrors(page);
  });
});
