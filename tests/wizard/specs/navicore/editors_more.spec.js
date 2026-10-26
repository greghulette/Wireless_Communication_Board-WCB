// The knob and switch editors edited for real, and a tier's ▶ Test (docs/hil_plan/NAVICORE.md §2, nct.edit.knob_modal,
// nct.edit.switch_modal, nct.edit.test_action, L1). nctool.noop_apply_every_editor proves an untouched Apply changes
// nothing; here the editors are driven the way a user drives them and Apply must write exactly the firmware's shape
// (saveKnobModal, config_tool/index.html ~16309; saveSwitchModal ~16534), Save must carry it, and the emulator keeps it.
// testTier (~15739) plays a whole tier through TEST_ACTION, honouring each action's pre-fire delay, skipping the
// recorder controls, refusing to run unconnected or under an upload, and aborting when an upload starts mid-sequence.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const cfg = (page, expr) => page.evaluate((e) => JSON.parse(JSON.stringify(eval(e))), expr);
const modalApply = (page) => page.locator('#modal .modal-footer .btn-success').click();
const modalClearAll = (page) => page.locator('#modal .modal-footer .btn-ghost', { hasText: 'Clear All' }).click();
// The tier's own ▶ Test, in its name header (not a row's ▶ Test, nctool.test_action_button).
const tierTest = (page, tier) => page.locator(`#tier-card-${tier} button[title^="Play this whole sequence"]`);
const out = (idx, f) => `#kn-out-${idx}-${f}`;

test.describe('the bench config', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.knob_editor_edits a disabled knob given the Maestro pass-through function is seeded one output; its target, channel, endpoints (typed in us, stored in quarter-us), centre-closed and idle release edit; a second output is added; channel, reverse, a smoothing profile and the switch override set; Follow 3 modes seeds modes 2-3 from mode 1 and a mode-2 output is removed alone; Apply writes the firmware\'s shape and pushes it to the board itself (a Save meanwhile is refused, not queued); an HCR-volume knob\'s audio channel and range edit; Clear All drops the knob and the next Save ships it as {} so the board resets it', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    expect(BENCH.knobs.S3).toMatchObject({ channel: 0, function: 0, outputs: [] });
    await page.evaluate(() => openKnobModal('S3'));
    await expect(page.locator('#modal')).toHaveClass(/open/);
    await expect(page.locator('#kn-function')).toHaveValue('0');
    await expect(page.locator('#kn-outputs-rows')).toHaveText(/Function disabled — outputs ignored\./);
    await expect(page.locator('#kn-add-output')).toBeDisabled();
    // Maestro pass-through: one output is seeded (Maestro 1, ch 0, 1000-2000 us, centre-closed, release at 1500 ms).
    await page.locator('#kn-function').selectOption('1');
    await expect(page.locator('#kn-outputs-rows .action-row')).toHaveCount(1);
    await expect(page.locator('#kn-output-count')).toHaveText(/^1 \/ \d+$/);
    await expect(page.locator(out(0, 'target'))).toHaveValue('1');
    await expect(page.locator(out(0, 'min'))).toHaveValue('1000');
    await expect(page.locator(out(0, 'max'))).toHaveValue('2000');
    await expect(page.locator(out(0, 'midclosed'))).toBeChecked();
    await expect(page.locator(out(0, 'release'))).toBeChecked();
    await expect(page.locator(out(0, 'releasems'))).toHaveValue('1500');
    await page.locator(out(0, 'target')).selectOption('2');
    await page.locator(out(0, 'ch')).selectOption('3');
    await page.locator(out(0, 'min')).fill('1100');
    await page.locator(out(0, 'max')).fill('1900');
    await page.locator(out(0, 'midclosed')).uncheck();
    await page.locator(out(0, 'releasems')).fill('2000');
    // A second output, with no idle release.
    await page.locator('#kn-add-output').click();
    await expect(page.locator('#kn-outputs-rows .action-row')).toHaveCount(2);
    await expect(page.locator(out(0, 'min')), 'the first row survived the add').toHaveValue('1100');
    await page.locator(out(1, 'target')).selectOption('2');
    await page.locator(out(1, 'ch')).selectOption('4');
    await page.locator(out(1, 'release')).uncheck();
    await expect(page.locator(out(1, 'releasems'))).toBeDisabled();
    // The source: channel 9, reversed, the "Slow" profile, the switch allowed to override it.
    await page.locator('#kn-channel').fill('9');
    await page.locator('#kn-reverse').check();
    await page.locator('#kn-smprof').selectOption('1');
    await expect(page.locator('#kn-smprof option:checked')).toHaveText('Slow');
    await page.locator('#kn-easeovr').check();
    // Follow 3 modes: modes 2 and 3 start as copies of mode 1; mode 2 loses its second output; the mode comes from SB.
    await expect(page.locator('#kn-editmode-tabs')).toBeHidden();
    await page.locator('#kn-modeaware').check();
    await expect(page.locator('#kn-editmode-tabs')).toBeVisible();
    await expect(page.locator('#kn-modesrc-wrap')).toBeVisible();
    await expect(page.locator('#kn-editmode-count-2')).toHaveText('2');
    await page.locator('#kn-modesrc').selectOption('1');
    await page.locator('#kn-editmode-tab-2').click();
    expect(await page.evaluate(() => editingKnobDraft.editMode)).toBe(2);
    await page.locator('#kn-outputs-rows .action-row[data-idx="1"] .btn-remove-action').click();
    await expect(page.locator('#kn-outputs-rows .action-row')).toHaveCount(1);
    await expect(page.locator('#kn-editmode-count-2')).toHaveText('1');
    await expect(page.locator('#kn-editmode-count-1')).toHaveText('2');
    emu.hold.add('SET_CONFIG');   // the board's ACK waits, so the overlapping-save guard can be seen below
    await modalApply(page);       // Apply writes the config and, connected, pushes it to the board itself (saveModalDispatch ~16580)
    await expect(page.locator('#modal')).not.toHaveClass(/open/);
    const first = { target: 2, maestroCh: 3, posMin: 4400, posMax: 7600, releaseIdleMs: 2000 };
    const second = { target: 2, maestroCh: 4, posMin: 4000, posMax: 8000, midClosed: true };
    const want = { channel: 9, function: 1, reverse: true, modeAware: true, modeSwitchOverride: 1, smoothProfile: 1, easeSwitchOverride: true,
                   outputs: [first, second], outputs2: [first], outputs3: [first, second] };
    expect(await cfg(page, 'config.knobs.S3')).toEqual(want);   // (S3 is an X20 knob: the X18's Axes table has no row for it)
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(Object.keys(sent.data), 'Apply saved on its own, the knobs branch alone').toEqual(['knobs']);
    expect(sent.data.knobs.S3).toEqual(want);
    // A Save while that one still awaits its ACK is refused, not queued.
    await page.evaluate(() => saveConfigToBoard());
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('A save is still in progress'));
    expect(emu.requests('SET_CONFIG')).toHaveLength(1);
    emu.release('SET_CONFIG');
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().knobs.S3).toMatchObject({ channel: 9, function: 1, reverse: true, modeAware: true, modeSwitchOverride: 1, smoothProfile: 1, easeSwitchOverride: true });
    expect(emu.config.toJSON().knobs.S3.outputs2).toEqual([first]);
    // An HCR-volume knob: the audio channel and the top of its range. Apply saves it the same way.
    expect(BENCH.knobs.S1.function).toBe(2);
    await page.evaluate(() => openKnobModal('S1'));
    await expect(page.locator('#kn-function')).toHaveValue('2');
    await expect(page.locator('#kn-outputs-title')).toHaveText('HCR volume outputs');
    await page.locator(out(0, 'target')).selectOption('2');
    await page.locator(out(0, 'max')).fill('80');
    await modalApply(page);
    expect(await cfg(page, 'config.knobs.S1')).toEqual({ channel: 5, function: 2, reverse: false, modeAware: false, modeSwitchOverride: -1,
                                                          outputs: [{ target: 2, maestroCh: 0, posMin: 0, posMax: 80 }] });
    const [, s1Save] = await T.waitRequests(emu, 'SET_CONFIG', 2);
    expect(s1Save.data.knobs.S1.outputs).toEqual([{ target: 2, maestroCh: 0, posMin: 0, posMax: 80 }]);
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    // Clear All drops the knob from the tool and saves nothing by itself. The next Save ships the knobs branch with S3
    // as {}: the firmware rebuilds only the labels present and leaves an absent one untouched (rc_config.h ~1738), so
    // a simply omitted S3 would keep its old wiring on the board and the next Refresh would bring it back
    // (_diffConfigBranches ~16767). The board resets it to its defaults.
    await page.evaluate(() => openKnobModal('S3'));
    await modalClearAll(page);
    await expect(page.locator('#modal')).not.toHaveClass(/open/);
    expect(await page.evaluate(() => 'S3' in config.knobs)).toBe(false);
    await page.waitForTimeout(300);
    expect(emu.requests('SET_CONFIG'), 'Clear All does not save').toHaveLength(2);
    await page.evaluate(() => saveConfigToBoard());
    const [, , cleared] = await T.waitRequests(emu, 'SET_CONFIG', 3);
    expect(Object.keys(cleared.data)).toEqual(['knobs']);
    expect(Object.keys(cleared.data.knobs).sort()).toEqual(Object.keys(sent.data.knobs).sort());
    expect(cleared.data.knobs.S3).toEqual({});
    expect(cleared.data.knobs.S1.outputs).toEqual([{ target: 2, maestroCh: 0, posMin: 0, posMax: 80 }]);
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().knobs.S3).toEqual({ channel: 0, function: 0, reverse: false, modeAware: false, modeSwitchOverride: -1, outputs: [] });
    T.expectNoPageErrors(page);
  });

  test('nctool.switch_editor_edits a switch zone opens its editor on the Down position; actions typed on Mid survive a change to 2 positions (which hides Mid), actions on Down and Up with a delay, a note and a second row are kept per position, Apply writes exactly that and pushes it to the board itself; back at 3 positions Mid shows what it kept (a position change writes through, so Cancel does not undo it: pinned); Clear All drops the switch and the next Save ships it as {} so the board resets it', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    expect(BENCH.switches.SB).toEqual({ channel: 9, positions: 3 });
    await page.locator('.switch-zone[data-sw="SB"]:visible').first().click();
    await expect(page.locator('#modal')).toHaveClass(/open/);
    await expect(page.locator('#modal-title')).toHaveText('Switch SB');
    await expect(page.locator('#sw-channel')).toHaveValue('9');
    await expect(page.locator('#sw-positions')).toHaveValue('3');
    await expect(page.locator('#tiers-container .tier-tab')).toHaveCount(3);
    await expect(page.locator('#tier-card-p0')).toBeVisible();
    await expect(page.locator('#tier-card-p1')).toBeHidden();
    // Mid first, then the switch becomes 2-position: Mid disappears from the editor but is not lost.
    await page.locator('#tiers-container .tier-tab[data-tier="p1"]').click();
    await expect(page.locator('#tier-card-p1')).toBeVisible();
    await page.locator('#fields-p1-0-cmd').fill(';S5HILMID');
    await page.locator('#tier-note-p1').fill('HIL mid');
    await page.locator('#sw-channel').fill('12');
    await page.locator('#sw-positions').selectOption('2');
    await expect(page.locator('#tiers-container .tier-tab')).toHaveCount(2);
    await expect(page.locator('#tier-card-p1')).toHaveCount(0);
    await expect(page.locator('#tier-card-p0')).toBeVisible();
    await page.locator('#fields-p0-0-cmd').fill(';S3HILDOWN');
    await page.locator('#tier-note-p0').fill('HIL down');
    await page.locator('#tiers-container .tier-tab[data-tier="p2"]').click();
    await page.locator('#fields-p2-0-cmd').fill(';S4HILUP');
    await page.locator('#delay-p2-0').fill('300');
    await page.locator('#tier-card-p2 .btn-add-action').click();
    await page.locator('#fields-p2-1-cmd').fill(';S4HILUP2');
    await page.locator('#note-p2-1').fill('second');
    await modalApply(page);
    const want = { channel: 12, positions: 2,
                   p0: [{ type: 'wcb_unicast', target: '1', cmd: ';S3HILDOWN' }], p0note: 'HIL down',
                   p1: [{ type: 'wcb_unicast', target: '1', cmd: ';S5HILMID' }], p1note: 'HIL mid',
                   p2: [{ type: 'wcb_unicast', target: '1', cmd: ';S4HILUP', delay: 300 }, { type: 'wcb_unicast', target: '1', cmd: ';S4HILUP2', note: 'second' }] };
    expect(await cfg(page, 'config.switches.SB')).toEqual(want);
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');   // Apply pushes the config to the board itself
    expect(Object.keys(sent.data)).toEqual(['switches']);
    expect(sent.data.switches.SB).toEqual(want);
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().switches.SB).toMatchObject({ channel: 12, positions: 2, p0note: 'HIL down', p1note: 'HIL mid' });
    expect(emu.config.toJSON().switches.SB.p2).toHaveLength(2);
    // Back at 3 positions, Mid is what it was. The position change is written into the config by the rebuild itself,
    // so Cancel leaves positions at 3 (pinned).
    await page.locator('.switch-zone[data-sw="SB"]:visible').first().click();
    await expect(page.locator('#sw-positions')).toHaveValue('2');
    await page.locator('#sw-positions').selectOption('3');
    await page.locator('#tiers-container .tier-tab[data-tier="p1"]').click();
    await expect(page.locator('#fields-p1-0-cmd')).toHaveValue(';S5HILMID');
    await expect(page.locator('#tier-note-p1')).toHaveValue('HIL mid');
    await page.locator('#modal .modal-footer .btn-ghost', { hasText: 'Cancel' }).click();
    expect(await cfg(page, 'config.switches.SB.positions')).toBe(3);
    // Clear All drops the switch from the tool; the next Save ships it as {} (the same rule as a knob's, above) and
    // the board resets it to its defaults: channel 9, 3 positions, no actions.
    await page.locator('.switch-zone[data-sw="SB"]:visible').first().click();
    await modalClearAll(page);
    expect(await page.evaluate(() => 'SB' in config.switches)).toBe(false);
    await page.evaluate(() => saveConfigToBoard());
    const [, cleared] = await T.waitRequests(emu, 'SET_CONFIG', 2);
    expect(Object.keys(cleared.data)).toEqual(['switches']);
    expect(cleared.data.switches.SB).toEqual({});
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect(emu.config.toJSON().switches.SB).toEqual({ channel: 9, positions: 3 });
    T.expectNoPageErrors(page);
  });

  test('nctool.tier_test_button a tier\'s ▶ Test is refused unconnected, with no actions, and while an upload is in flight; connected it fires the tier\'s actions through TEST_ACTION the way a press does, each at its delay after the start (a delayed action does not hold back the ones after it), dropping delay and note from what it sends, names the sequence, disables itself meanwhile; a recorder control is skipped and counted; an upload starting mid-sequence aborts it; a switch position tests the same way with an unsaved edit', async ({ page, emu }) => {
    await T.openTool(page);
    // Unconnected.
    await page.locator('#assignment-list > div').nth(9).click();   // button 10: three actions, one delayed, one noted
    await tierTest(page, 1).click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Not connected — connect to a NaviCore to test'));
    expect(await T.termLines(page, /· test: not connected/)).toHaveLength(1);
    await page.evaluate(() => closeModal());
    await T.connectUsb(page, emu);
    // No actions: button 2 in mode 2 has no mapping.
    await page.evaluate(() => setEditingMode(2));
    await page.locator('#assignment-list > div').nth(1).click();
    await tierTest(page, 1).click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No actions to test in this sequence'));
    await page.evaluate(() => { closeModal(); setEditingMode(1); });
    // An upload in flight.
    await page.locator('#assignment-list > div').nth(9).click();
    await page.evaluate(() => { _bridgeUploadInFlight = true; });
    await tierTest(page, 1).click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Upload in progress — test again when it finishes'));
    await page.evaluate(() => { _bridgeUploadInFlight = false; });
    expect(emu.requests('TEST_ACTION')).toEqual([]);
    // The whole tier as a press fires it (rcExecuteAction schedules each delay from the press): A and C at once (C's
    // note dropped), B 500 ms after the start.
    await page.locator('#tier-note-1').fill('HIL seq');
    const btn = tierTest(page, 1);
    await btn.click();
    expect(await T.termLines(page, /▶ \[test\] playing 3 action\(s\) — "HIL seq"/)).toHaveLength(1);
    await expect(btn).toBeDisabled();
    await expect(btn).toHaveClass(/fired/);
    await T.waitRequests(emu, 'TEST_ACTION', 2);
    expect(emu.requests('TEST_ACTION').map((m) => m.action.cmd)).toEqual([';S2HIL110A', ';W1;S3HIL110C']);
    await page.clock.fastForward(600);
    await T.waitRequests(emu, 'TEST_ACTION', 3);
    expect(emu.requests('TEST_ACTION').map((m) => m.action)).toEqual([
      { type: 'wcb_unicast', target: '1', cmd: ';S2HIL110A' },
      { type: 'wcb_broadcast', cmd: ';W1;S3HIL110C' },
      { type: 'wcb_unicast', target: '2', cmd: ';S2HIL110B' },
    ]);
    expect(await T.termLines(page, /◀ \[test\] sequence complete — 3 fired$/)).toHaveLength(1);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Tested “HIL seq” — 3 actions fired'));
    await expect(btn).toBeEnabled();
    // An upload that starts during B's delay aborts the rest (A and C have gone).
    await btn.click();
    await T.waitRequests(emu, 'TEST_ACTION', 5);
    await page.evaluate(() => { _bulkActive = true; });
    await page.clock.fastForward(600);
    expect(await T.termLines(page, /· \[test\] aborted — a config\/library upload started mid-test/)).toHaveLength(1);
    expect(await T.termLines(page, /◀ \[test\] sequence complete — 2 fired$/)).toHaveLength(1);
    await page.evaluate(() => { _bulkActive = false; });
    expect(emu.requests('TEST_ACTION')).toHaveLength(5);
    expect(emu.requests('SET_CONFIG'), 'Test never saves').toEqual([]);
    await page.evaluate(() => closeModal());
    // A recorder control is skipped, not sent.
    await page.locator('#assignment-list > div').nth(6).click();   // button 7: t1 plays a clip
    await tierTest(page, 1).click();
    expect(await T.termLines(page, /· \[test\] step 1: "play" is a recorder control — skipped/)).toHaveLength(1);
    expect(await T.termLines(page, /◀ \[test\] sequence complete — 0 fired, 1 skipped/)).toHaveLength(1);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Tested sequence — 0 actions fired, 1 skipped'));
    expect(emu.requests('TEST_ACTION')).toHaveLength(5);
    await page.evaluate(() => closeModal());
    // A switch position: the row as typed, unsaved, fires.
    await page.locator('.switch-zone[data-sw="SB"]:visible').first().click();
    await page.locator('#fields-p0-0-cmd').fill(';S3HILSW');
    await tierTest(page, 'p0').click();
    await T.waitRequests(emu, 'TEST_ACTION', 6);
    expect(emu.requests('TEST_ACTION')[5].action).toEqual({ type: 'wcb_unicast', target: '1', cmd: ';S3HILSW' });
    expect(emu.requests('SET_CONFIG')).toEqual([]);
    T.expectNoPageErrors(page);
  });
});
