// The Config window's tabs edited for real and saved (docs/hil_plan/NAVICORE.md §2, nct.edit.channels_tab,
// nct.edit.logical_buttons, nct.edit.tx_model, nct.edit.maestro_locations, nct.edit.audio_dests, nct.edit.wled_slots,
// nct.edit.smoothing, nct.edit.serial_ports, nct.edit.general, L1). nctool.apply_all_keys and nctool.no_phantom_diff cover
// loading and an untouched Save; here each pane gets an edit a user would make, and the Save that follows must carry
// exactly the branches those edits touched (_diffConfigBranches, config_tool/index.html ~16750) - the emulator stores
// what it is sent, so a Refresh shows what the board would keep.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

async function openConfig(page, tab) {
  await page.locator('#btn-hwsetup').click();
  await expect(page.locator('#hwsetup-modal')).toHaveClass(/open/);
  if (tab) await page.locator(`#cfg-tabs .cfg-tab[data-tab="${tab}"]`).click();
}
// A text or number field whose handler is onchange: typed, then the change a blur would fire.
async function setField(loc, value) {
  await loc.fill(String(value));
  await loc.dispatchEvent('change');
}
// A range slider: Playwright cannot fill one.
async function setRange(loc, value) {
  await loc.evaluate((el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }, String(value));
}
async function save(page, emu, n = 1) {
  await page.locator('#btn-hwsetup-save').click();
  const sent = await T.waitRequests(emu, 'SET_CONFIG', n);
  await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
  return sent[n - 1];
}
const cfg = (page, expr) => page.evaluate((e) => JSON.parse(JSON.stringify(eval(e))), expr);

test.describe('the Config window on the bench config', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.channels_tab the Buttons, Switches and Axes tables and the Logical Buttons tab write into the config (a label, a trigger centre as a +-10 band, a position count, a channel, a reverse flag, a logical band); the button editor\'s trigger field mirrors the table; Save carries thresholds, switches and knobs; Rebuild resets every band to the model defaults, logical ones to 0/0, without asking (pinned)', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openConfig(page, 'channels');
    // Buttons: B1's label and trigger centre (band = centre +-10).
    const b1 = page.locator('#thresh-table tbody tr').first();
    await expect(b1.locator('td').first()).toHaveText('1');
    await setField(b1.locator('input[type=text]'), 'HIL B1');
    await setField(b1.locator('input[type=number]'), 1500);
    expect(await cfg(page, 'config.thresholds[0]')).toEqual({ id: 1, label: 'HIL B1', minPwm: 1490, maxPwm: 1510 });
    // Switches: SB to 2 positions, SC to channel 20.
    await page.locator('#switches-table select[data-sw="SB"]').selectOption('2');
    await setField(page.locator('#switches-table input[data-sw="SC"]'), 20);
    expect(await cfg(page, '[config.switches.SB.positions, config.switches.SC.channel]')).toEqual([2, 20]);
    // Axes: S1 to channel 21 and reversed.
    await setField(page.locator('#axes-table input[data-knob-ch="S1"]'), 21);
    await page.locator('#axes-table input[data-knob-rev="S1"]').check();
    expect(await cfg(page, '[config.knobs.S1.channel, config.knobs.S1.reverse]')).toEqual([21, true]);
    // Logical Buttons: slot 22 named and given a band.
    await page.locator('#cfg-tabs .cfg-tab[data-tab="logical"]').click();
    await setField(page.locator('#logical-list .logical-label[data-id="22"]'), 'HIL logic');
    await setField(page.locator('#logical-list .logical-center[data-id="22"]'), 1300);
    expect(await cfg(page, 'config.thresholds[21]')).toEqual({ id: 22, label: 'HIL logic', minPwm: 1290, maxPwm: 1310 });
    const sent = await save(page, emu);
    expect(Object.keys(sent.data).sort()).toEqual(['knobs', 'switches', 'thresholds']);
    expect(sent.data.thresholds[0]).toEqual({ id: 1, label: 'HIL B1', minPwm: 1490, maxPwm: 1510 });
    expect(sent.data.thresholds[21]).toEqual({ id: 22, label: 'HIL logic', minPwm: 1290, maxPwm: 1310 });
    expect([sent.data.switches.SB.positions, sent.data.switches.SC.channel, sent.data.knobs.S1.channel, sent.data.knobs.S1.reverse]).toEqual([2, 20, 21, true]);
    const board = emu.config.toJSON();
    expect([board.thresholds[0].label, board.thresholds[0].minPwm, board.thresholds[21].maxPwm, board.switches.SC.channel, board.knobs.S1.reverse]).toEqual(['HIL B1', 1490, 1310, 20, true]);
    // The button editor's trigger field is the same band: B1 to 1600 there shows in the table's title.
    await page.evaluate(() => closeHwSetup());
    await page.locator('#assignment-list > div').first().click();
    // A physical slot's title is the model's label (getBtnLabel): the typed label lives in the Buttons table alone.
    await expect(page.locator('#modal-title')).toHaveText(/^Button 1: B1/);
    await setField(page.locator('#btn-pwm-center'), 1600);
    expect(await cfg(page, 'config.thresholds[0]')).toEqual({ id: 1, label: 'HIL B1', minPwm: 1590, maxPwm: 1610 });
    await page.evaluate(() => closeModal());
    await openConfig(page, 'channels');
    await expect(b1.locator('input[type=number]')).toHaveAttribute('title', /min 1590 \/ max 1610/);
    // Rebuild: every physical band back to the model's defaults (centre +-12), the logical band gone, no question asked.
    await page.locator('#btn-thresh-rebuild').click();
    expect(dialogs, 'Rebuild asks nothing (pinned)').toEqual([]);
    const after = await cfg(page, 'config.thresholds');
    expect(after[0]).toEqual({ id: 1, label: 'B1', minPwm: 1799, maxPwm: 1823 });
    expect(after[21]).toEqual({ id: 22, label: 'Logical 1', minPwm: 0, maxPwm: 0 });
    expect(after).toHaveLength(36);
    await expect(b1.locator('input[type=text]')).toHaveValue('B1');
    T.expectNoPageErrors(page);
  });

  test('nctool.tx_model_switch the Transmitter tab\'s model select re-draws the Channels tables and the assignment cards for that model\'s controls (the Twin X-Lite has no LS/RS, slots 5-6 or SG-SJ), the X20 shows its 3-axis gimbals option, which adds J5/J6 and slots 19/20 when ticked, and Save sends txModel and threeAxisGimbals', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openConfig(page, 'transmitter');
    await expect(page.locator('#tx-model-select')).toHaveValue('0');
    await expect(page.locator('#cfg-x20-options')).toBeHidden();
    const models = await page.evaluate(() => Object.fromEntries(Object.values(TX_MODELS).map((m) => [m.id, { buttons: m.buttons, switches: m.switches, knobs: m.knobs, slug: m.cssSlug }])));
    const switchRows = () => page.locator('#switches-table tbody tr td:first-child').allInnerTexts();
    const axisRows = () => page.locator('#axes-table tbody tr td:first-child b').allInnerTexts();
    const buttonRows = () => page.locator('#thresh-table tbody tr td:first-child').allInnerTexts().then((a) => a.map(Number));
    expect(await switchRows()).toEqual(models[0].switches);
    // Twin X-Lite.
    await page.locator('#tx-model-select').selectOption('1');
    expect(await cfg(page, 'config.txModel')).toBe(1);
    await expect(page.locator('body')).toHaveClass(new RegExp(`tx-show-${models[1].slug}`));
    expect(await switchRows()).toEqual(models[1].switches);
    expect(await axisRows()).toEqual(models[1].knobs);
    expect(await buttonRows()).toEqual(models[1].buttons);
    expect(models[1].knobs).not.toContain('LS');
    expect(models[1].buttons).not.toContain(5);
    await expect(page.locator('#tx-model-help')).toHaveText('Click Save (Config window footer) to push this to the board.');
    // X20 with the 3-axis gimbals.
    await page.locator('#tx-model-select').selectOption('2');
    await expect(page.locator('#cfg-x20-options')).toBeVisible();
    await expect(page.locator('#cfg-three-axis-gimbals')).not.toBeChecked();
    expect(await axisRows()).toEqual(models[2].knobs.filter((k) => k !== 'J5' && k !== 'J6'));
    expect(await buttonRows()).toEqual(models[2].buttons.filter((b) => b !== 19 && b !== 20));
    await page.locator('#cfg-three-axis-gimbals').check();
    expect(await cfg(page, 'config.threeAxisGimbals')).toBe(true);
    await expect(page.locator('body')).toHaveClass(/three-axis-on/);
    expect(await axisRows()).toEqual(models[2].knobs);
    expect(await buttonRows()).toEqual(models[2].buttons);
    const sent = await save(page, emu);
    expect(sent.data).toEqual({ txModel: 2, threeAxisGimbals: true });
    expect([emu.config.toJSON().txModel, emu.config.toJSON().threeAxisGimbals]).toEqual([2, true]);
    // Back to the X18 (the gimbals box first: it is hidden for any other model).
    await page.locator('#cfg-three-axis-gimbals').uncheck();
    await page.locator('#tx-model-select').selectOption('0');
    await expect(page.locator('#cfg-x20-options')).toBeHidden();
    expect(await switchRows()).toEqual(models[0].switches);
    await save(page, emu, 2);
    expect([emu.config.toJSON().txModel, emu.config.toJSON().threeAxisGimbals]).toEqual([0, false]);
    T.expectNoPageErrors(page);
  });

  test('nctool.maestro_locations_xml the Maestro tab\'s rows set a slot\'s location and Pololu device number; a Control Center settings file imported into a slot stores its servo channels\' names and endpoints (quarter-us) in the config and snaps the passthrough outputs on that slot to them, keeping a reversed output reversed; the endpoint editor adds a channel by hand in us; Save carries maestros and knobs', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    // A passthrough knob on slot 2 to see the import snap its ranges: one output forward, one reversed.
    await page.evaluate(() => {
      config.knobs.S2 = { ...config.knobs.S2, function: 1, outputs: [{ target: 2, maestroCh: 0, posMin: 4000, posMax: 8000 }, { target: 2, maestroCh: 1, posMin: 8000, posMax: 4000 }] };
    });
    await openConfig(page, 'maestro');
    const row = (i) => page.locator(`#maestro-locations select[data-mae-idx="${i}"][data-mae-field="type"]`);
    await expect(row(0)).toHaveValue('1');
    await expect(row(1)).toHaveValue('2');
    // Slot 2 becomes local, slot 3's device number changes.
    await row(1).selectOption('1');
    await setField(page.locator('#maestro-locations input[data-mae-idx="2"][data-mae-field="device"]'), 12);
    expect(await cfg(page, '[config.maestros[1].type, config.maestros[2].device]')).toEqual([1, 12]);
    await expect(page.locator('#maestro-locations [data-mae-read="GET"][data-mae-slot="2"]'), 'a local slot gets the live read controls').toBeVisible();
    // Import a settings file into slot 2: channels 0 and 1 are servos, 2 is an input (skipped), 3 has no range (skipped).
    const xml = '<?xml version="1.0"?><UscSettings><Channels>'
      + '<Channel name="Dome" mode="Servo" min="3968" max="8000"/>'
      + '<Channel name="Panel" mode="ServoMultiplied" min="4200" max="7800"/>'
      + '<Channel name="Sense" mode="Input" min="0" max="1024"/>'
      + '<Channel name="Dead" mode="Servo" min="6000" max="6000"/>'
      + '</Channels></UscSettings>';
    await T.chooseFile(page, () => page.locator('#maestro-locations [data-mae-import="2"]').click(), 'maestro2.txt', xml);
    await expect.poll(() => dialogs.length).toBe(1);
    // The bench config has three more passthrough outputs on slot 2 (other knobs), snapped as well: five in all.
    expect(dialogs[0].message).toMatch(/^Imported 2 servo channel\(s\) for Maestro 2\.\nSet the range on 5 passthrough output\(s\)\./);
    expect(await cfg(page, 'config.maestros[1].channels')).toEqual([{ ch: 0, name: 'Dome', min: 3968, max: 8000 }, { ch: 1, name: 'Panel', min: 4200, max: 7800 }]);
    expect(await cfg(page, 'config.knobs.S2.outputs'), 'snapped to the endpoints, the reversed one still reversed').toEqual([
      { target: 2, maestroCh: 0, posMin: 3968, posMax: 8000 }, { target: 2, maestroCh: 1, posMin: 7800, posMax: 4200 }]);
    await expect(page.locator('#maestro-locations')).toContainText('2 ch imported');
    // The endpoint editor on slot 3: a channel typed in microseconds is stored in quarter-microseconds.
    await page.locator('#maestro-locations [data-mae-edit="3"]').click();
    const ed = page.locator('.modal-overlay.open').last();
    await expect(ed.locator('.modal-title')).toHaveText('Maestro 3 — channel endpoints');
    await ed.locator('.mc-ch').first().fill('4');
    await ed.locator('.mc-nm').first().fill('Lifter');
    await ed.locator('.mc-min').first().fill('1000');
    await ed.locator('.mc-max').first().fill('2000');
    await ed.locator('#mae-ch-ok').click();
    expect(await cfg(page, 'config.maestros[2].channels')).toEqual([{ ch: 4, name: 'Lifter', min: 4000, max: 8000 }]);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Maestro 3 endpoints updated'));
    // A bad row is refused and nothing changes.
    await page.locator('#maestro-locations [data-mae-edit="3"]').click();
    const ed2 = page.locator('.modal-overlay.open').last();
    await ed2.locator('.mc-min').first().fill('2500');
    await ed2.locator('#mae-ch-ok').click();
    expect(dialogs.at(-1).message).toBe('channel 4: min must be below max');
    await ed2.locator('#mae-ch-cancel').click();
    expect(await cfg(page, 'config.maestros[2].channels')).toEqual([{ ch: 4, name: 'Lifter', min: 4000, max: 8000 }]);
    const sent = await save(page, emu);
    expect(Object.keys(sent.data).sort()).toEqual(['knobs', 'maestros']);
    expect(sent.data.maestros[1]).toEqual({ type: 1, device: 2, channels: [{ ch: 0, name: 'Dome', min: 3968, max: 8000 }, { ch: 1, name: 'Panel', min: 4200, max: 7800 }] });
    expect(sent.data.maestros[2]).toEqual({ type: 2, device: 12, channels: [{ ch: 4, name: 'Lifter', min: 4000, max: 8000 }] });
    const board = emu.config.toJSON();
    expect([board.maestros[1].type, board.maestros[2].device, board.maestros[2].channels[0].name]).toEqual([1, 12, 'Lifter']);
    T.expectNoPageErrors(page);
  });

  test('nctool.editors_misc one real edit on each of the Audio, WLED, Smoothing, Serial Ports and General panes writes into the config, the General tab\'s hold threshold is clamped above the tap window and says so, mode and stats reporting toggle their fields, and one Save carries exactly those branches with the clamped values', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    // Audio: the HCR to a local port, the MP3 Trigger to a WCB, the DFPlayer to a local port.
    await openConfig(page, 'audio');
    await page.locator('#hcr-transport').selectOption('serial');
    await page.locator('#hcr-port').selectOption('S4');
    await page.locator('#mp3-transport').selectOption('wcb');
    await page.locator('#mp3-target').fill('7');
    await page.locator('#dfp-transport').selectOption('serial');
    await page.locator('#dfp-port').selectOption('S5');
    expect(await cfg(page, '[config.hcrDest.transport, config.hcrDest.port, config.mp3Dest.transport, config.mp3Dest.target, config.dfpDest.transport, config.dfpDest.port]'))
      .toEqual(['serial', 'S4', 'wcb', '7', 'serial', 'S5']);
    // WLED: slot 2 as WLED 2 on local S4, slot 3 as WLED 3 on WCB 5.
    await page.locator('#cfg-tabs .cfg-tab[data-tab="wled"]').click();
    const wled = (i, f) => page.locator(`#wled-slots [data-wled-idx="${i}"][data-wled-field="${f}"]`);
    await setField(wled(1, 'id'), 2);
    await wled(1, 'mode').selectOption('local');
    await wled(1, 'port').selectOption('4');
    await setField(wled(2, 'id'), 3);
    await wled(2, 'mode').selectOption('remote');
    await setField(wled(2, 'wcb'), 5);
    expect(await cfg(page, 'config.wledSlots.slice(0, 3)')).toEqual([BENCH.wledSlots[0], { id: 2, port: 4, wcb: 0, configured: true }, { id: 3, port: 0, wcb: 5, configured: true }]);
    // Smoothing: profile 3 named, one channel added to Maestro 1 with its speed eased to 50 (stored as the Maestro limit).
    await page.locator('#cfg-tabs .cfg-tab[data-tab="smoothing"]').click();
    const bar = page.locator('#smoothing-editor > div').first();
    await bar.locator('select').selectOption('2');
    await setField(bar.locator('input[type=text]'), 'HIL ease');   // its change re-draws the pane: before the next click
    await page.locator('#smoothing-editor button', { hasText: '+ add channel' }).first().click();
    const chRow = page.locator('#smoothing-editor select').nth(1);     // the profile select is the first
    await chRow.selectOption('5');
    await setRange(page.locator('#smoothing-editor input[type=range][data-sk="spd"]').first(), 50);
    expect(await cfg(page, 'config.smoothProfiles[2]')).toEqual({ name: 'HIL ease', entries: [{ mid: 1, ch: 5, spd: 125, acc: 0 }] });
    // Serial Ports: S4's rate, a label on S3, broadcast out on S4 and in on S5.
    await page.locator('#cfg-tabs .cfg-tab[data-tab="serial"]').click();
    await page.locator('#aux-baud-s4').selectOption('19200');
    await setField(page.locator('#slabel-S3'), 'HIL dome lights');
    await page.locator('#bcout-S4').check();
    await page.locator('#bcin-S5').check();
    expect(await cfg(page, '[config.auxBaud.S4, config.serialLabels.S3, config.serialBcast.S4.out, config.serialBcast.S5.in]')).toEqual([19200, 'HIL dome lights', true, true]);
    // General: the hold threshold typed below tapWindow + 100 is flagged and saved as tapWindow + 250.
    await page.locator('#cfg-tabs .cfg-tab[data-tab="general"]').click();
    await page.locator('#tapwin-input').fill('600');
    await page.locator('#holdms-input').fill('650');
    await expect(page.locator('#holdms-hint')).toHaveText(/Must be at least 700 ms — the 600 ms tap window fires first\. Will be saved as 850 ms\./);
    await page.locator('#switchsettle-input').fill('120');
    await setRange(page.locator('#chrate-input'), 10);
    await expect(page.locator('#chrate-val')).toHaveText('10 Hz');
    await page.locator('#sbus-out-toggle').uncheck();
    await expect(page.locator('#modereport-fields')).toBeVisible();
    await page.locator('#modereport-enabled').uncheck();
    await expect(page.locator('#modereport-fields')).toBeHidden();
    await page.locator('#stats-wcb').fill('3');
    await page.locator('#stats-enabled').uncheck();
    await expect(page.locator('#stats-fields')).toBeHidden();
    await page.locator('#stats-enabled').check();
    await expect(page.locator('#stats-fields')).toBeVisible();
    const sent = await save(page, emu);
    expect(Object.keys(sent.data).sort()).toEqual(['auxBaud', 'chRateHz', 'dfpDest', 'hcrDest', 'holdMs', 'modeReport', 'mp3Dest', 'sbusOutEnabled',
                                                   'serialBcast', 'serialLabels', 'smoothProfiles', 'statsReport', 'switchSettleMs', 'tapWindowMs', 'wledSlots']);
    expect(sent.data).toMatchObject({ tapWindowMs: 600, holdMs: 850, switchSettleMs: 120, chRateHz: 10, sbusOutEnabled: false,
                                      modeReport: { enabled: false, wcb: 2, template: ';V,MODE,{mode}' }, statsReport: { enabled: true, wcb: 3 },
                                      auxBaud: { S3: 115200, S4: 19200, S5: 9600, maestro: 57600 }, serialLabels: { S3: 'HIL dome lights' },
                                      hcrDest: { transport: 'serial', port: 'S4' }, mp3Dest: { transport: 'wcb', target: '7' }, dfpDest: { transport: 'serial', port: 'S5' } });
    expect(sent.data.serialBcast).toEqual({ S3: { out: false, in: false }, S4: { out: true, in: false }, S5: { out: false, in: true } });
    expect(sent.data.smoothProfiles[2]).toEqual({ name: 'HIL ease', entries: [{ mid: 1, ch: 5, spd: 125, acc: 0 }] });
    expect(sent.data.wledSlots[1]).toEqual({ id: 2, port: 4, wcb: 0, configured: true });
    const board = emu.config.toJSON();
    expect([board.holdMs, board.tapWindowMs, board.chRateHz, board.sbusOutEnabled, board.modeReport.enabled, board.statsReport.wcb, board.auxBaud.S4]).toEqual([850, 600, 10, false, false, 3, 19200]);
    // A second Save has nothing left to send.
    await page.locator('#btn-hwsetup-save').click();
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('No changes to save'));
    expect(emu.requests('SET_CONFIG')).toHaveLength(1);
    T.expectNoPageErrors(page);
  });
});
