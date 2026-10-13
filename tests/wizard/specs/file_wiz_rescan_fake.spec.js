// Needs no board: loading a system file, and the guided setup wizard's navigation, its saving of each step and its
// Apply (Wizard/app.js) - medium-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md,
// rows in docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). Fake boards in the
// real page (lib/fake.js), the wizard driven through its own buttons and step inputs. Runs standalone, in CI, and under
// the harness by id (s30_wizard.py).
//
// Nothing here is a real credential: the mesh passwords are dummies.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, pullFake } = require('../lib/fake');
const { setup, toastMark, toastsSince } = require('../lib/rescan');

// What exportSystemFile would download (as app_fake.spec.js captures it).
const exportFile = (page) => page.evaluate(async () => {
  const create = URL.createObjectURL, click = HTMLAnchorElement.prototype.click;
  let blob = null;
  URL.createObjectURL = (b) => { blob = b; return 'blob:hil-export'; };
  HTMLAnchorElement.prototype.click = function () {};
  try { exportSystemFile(); } finally { URL.createObjectURL = create; HTMLAnchorElement.prototype.click = click; }
  return blob ? blob.text() : null;
});
const conflictRows = (page) => page.$$eval('#general-conflict-body tbody tr td:first-child', (tds) => tds.map((t) => t.textContent));
const modalOpen = (page) => page.evaluate(() => document.getElementById('general-conflict-modal').classList.contains('open'));

test('wizard.file_fake_load Load File sets General and a "loaded file" baseline from the file - ETM and NaviCore included, so the same boards pulled afterwards open no keep/use modal - renders its boards, derives the controller and toasts the droid and board count; a file with no sections loads as an empty system, and a failed parse toasts', async ({ page }) => {
  // loadSystemFileContent (app.js:9551-9611), syncGeneralFromConfig (9228-9306). Two defects behind one symptom - a
  // pull of a board the file describes opening the keep/use modal - fixed 2026-10-12: a pull never copied the board's
  // ETM values into General's config (syncGeneralFromConfig set only the inputs), so the export wrote the page's
  // defaults into [GENERAL]; and a load handed syncGeneralFromConfig no NaviCore fields, so General was left with the
  // controller off and the first General edit put that in the baseline. Row file.load.
  await setup(page);
  await pullFake(page, 1, board(['CONTROLLER,ON,15', 'LABEL,S5,Front', 'ETM,TIMEOUT,600']));
  await pullFake(page, 2, board(['CONTROLLER,ON,15', 'ETM,TIMEOUT,600'], { wcb: 2 }));
  await page.waitForTimeout(400);
  expect(await modalOpen(page)).toBe(false);
  await page.evaluate(() => { systemConfig.droidName = 'HIL'; });
  const file = await exportFile(page);

  await setup(page);
  let m = await toastMark(page);
  await page.evaluate((t) => loadSystemFileContent(t), file);
  expect(await toastsSince(page, m)).toContain('Loaded: HIL — 2 board(s)');
  expect(await page.evaluate(() => ({
    source: generalBaseline.sourceBoard, etm: [generalBaseline.fields.etmEnabled, generalBaseline.fields.etmTimeout],
    navi: [generalBaseline.fields.navicoreEnabled, generalBaseline.fields.navicoreId],
    controller: systemConfig.general.controller, naviId: document.getElementById('g-navicore-id').value,
    general: [document.getElementById('g-wcbq').value, document.getElementById('g-mac3').value, document.getElementById('g-etm-timeout').value],
    label: document.getElementById('b1-s5-label').value,
  }))).toEqual({ source: 'file', etm: [true, 600], navi: [true, 15], controller: 'navicore', naviId: '15',
                 general: ['2', '4B', '600'], label: 'Front' });
  // The same board pulled - after a General edit, which re-derives the baseline from General: nothing differs from
  // the file, so no modal.
  await page.evaluate(() => { const el = document.getElementById('g-mac2'); el.value = '06'; el.dispatchEvent(new Event('input')); el.value = '05'; el.dispatchEvent(new Event('input')); });
  await pullFake(page, 1, board(['CONTROLLER,ON,15', 'LABEL,S5,Front', 'ETM,TIMEOUT,600']));
  await page.waitForTimeout(400);
  expect(await modalOpen(page), 'the file\'s own values are no conflict').toBe(false);
  expect(await page.evaluate(() => [systemConfig.general.specialPeer, systemConfig.general.etm.timeoutMs])).toEqual([true, 600]);
  // One that does differ: the modal compares it with "loaded file", naming only what differs.
  await pullFake(page, 2, board(['CONTROLLER,ON,15', 'ETM,TIMEOUT,600', 'MAC,3,4C'], { wcb: 2 }));
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible();
  expect(await page.locator('#general-conflict-body').textContent()).toContain('than loaded file');
  expect(await conflictRows(page)).toEqual(['MAC Octet 3']);

  // A text with no [GENERAL] or [WCB<n>] section is not refused: it loads as an empty system (today) - General back
  // to the defaults, no boards.
  await setup(page);
  await pullFake(page, 1, board());
  m = await toastMark(page);
  await page.evaluate(() => loadSystemFileContent('this is not a system file\njust text'));
  expect(await toastsSince(page, m)).toContain('Loaded: unnamed droid — 0 board(s)');
  expect(await page.evaluate(() => [document.getElementById('g-wcbq').value, document.getElementById('g-mac3').value, generalBaseline.sourceBoard]))
    .toEqual(['1', '00', 'file']);
  // A parse that throws: said, nothing loaded.
  m = await toastMark(page);
  await page.evaluate(() => loadSystemFileContent(null));
  expect(await toastsSince(page, m)).toContain('Failed to parse file:');
  expect(page.wizErrors).toEqual([]);
});

// The setup wizard's current step key.
const step = (page) => page.evaluate(() => wizardState.steps[wizardState.currentIdx]);
// Open the setup wizard (the page's openWizard, not lib/wizard.js's).
const openSetup = (page) => page.evaluate(() => { window.openWizard(); });

test('wizard.wiz_fake_navigation the setup wizard: Next validates then saves a step, a card click advances 200 ms later - once, and not after Back - Review\'s button reads Apply & Connect, and leaving the Firmware step stamps each WCB slot\'s flash mode (factory with firmware, erase without) and none for a client slot', async ({ page }) => {
  // wizardNext / wizardBack (app.js:10590-10639), wizardRenderStep (10642-10736), wizardSelectQty /
  // wizardAdvanceAfterChoice (11677-11710). The wizard always erases NVS (wizardSaveStep 'firmware'), so a slot is
  // stamped 'factory' or 'erase'; 'update' and 'configure' are never chosen here. Row wiz.navigation.
  await setup(page);
  await openSetup(page);
  expect(await step(page)).toBe('welcome');
  await page.locator('#wizard-next-btn').click();
  expect(await step(page)).toBe('quantity');

  // A quantity card: highlighted at once, the step changes 200 ms later.
  await page.locator('.wizard-qty-btn', { hasText: /^2$/ }).click();
  expect(await step(page), 'not before 200 ms').toBe('quantity');
  await page.waitForFunction(() => wizardState.steps[wizardState.currentIdx] === 'identity', null, { timeout: 2000 });
  expect(await page.evaluate(() => [wizardState.quantity, wizardState.boards.length])).toEqual([2, 2]);

  // Back cancels a pending advance: a card clicked, then Back at once - the wizard stays where Back took it.
  await page.locator('#wizard-back-btn').click();
  expect(await step(page)).toBe('quantity');
  await page.evaluate(() => { document.querySelectorAll('.wizard-qty-btn')[2].click(); wizardBack(); });
  await page.waitForTimeout(400);
  expect([await step(page), await page.evaluate(() => wizardState.quantity)]).toEqual(['welcome', 3]);
  // Two cards in a row: one advance, with the second choice.
  await page.locator('#wizard-next-btn').click();
  await page.evaluate(() => { const b = document.querySelectorAll('.wizard-qty-btn'); b[1].click(); b[0].click(); });
  await page.waitForTimeout(500);
  expect([await step(page), await page.evaluate(() => wizardState.quantity)]).toEqual(['identity', 1]);

  // Next validates first: the control step with no choice says so and stays.
  await page.evaluate(() => { wizardState.currentIdx = wizardState.steps.indexOf('control'); wizardRenderStep(); });
  const m = await toastMark(page);
  await page.locator('#wizard-next-btn').click();
  expect(await step(page)).toBe('control');
  expect(await toastsSince(page, m)).toContain('Please choose a control system');

  // Review: the button reads Apply & Connect and the export is offered; Next goes to the Firmware step.
  await page.evaluate(() => {
    Object.assign(wizardState, { controlSystem: 'other', maestroEnabled: false, quantity: 3 });
    wizardInitBoards(3);
    wizardState.boards[1].type = 'client';
    wizardState.steps = buildWizardSteps();
    wizardState.currentIdx = wizardState.steps.indexOf('review');
    wizardRenderStep();
  });
  expect(await page.locator('#wizard-next-btn').textContent()).toBe('Apply & Connect →');
  expect(await page.locator('#wizard-export-btn').count()).toBe(1);
  await page.locator('#wizard-next-btn').click();
  expect(await step(page)).toBe('firmware');
  expect(await page.locator('#wizard-export-btn').count()).toBe(0);

  // Firmware: Yes, then Next - every WCB slot stamped factory (and to push after), the client slot not at all.
  await page.locator('.wizard-choice-btn', { hasText: 'Yes, upload firmware' }).click();
  await page.evaluate(() => { for (const k of Object.keys(boardFlashMode)) delete boardFlashMode[k]; });
  await page.locator('#wizard-next-btn').click();
  expect(await step(page)).toBe('connect');
  expect(await page.evaluate(() => [boardFlashMode[1], boardFlashMode[2] ?? null, boardFlashMode[3], boardAutoPushAfterFlash[1],
                                    document.getElementById('wizard-next-btn').style.display]))
    .toEqual(['factory', null, 'factory', true, 'none']);
  // (The Connect step picks its mode on a 0 ms timer: let it run before closing, or it lands in the next session.)
  await page.waitForFunction(() => wizardState.connectMode === 'seq');
  // No firmware: erase.
  await page.evaluate(() => { closeWizard(); });
  await openSetup(page);
  await page.evaluate(() => {
    Object.assign(wizardState, { controlSystem: 'other', maestroEnabled: false });
    wizardState.steps = buildWizardSteps();
    wizardState.currentIdx = wizardState.steps.indexOf('firmware');
    wizardRenderStep();
  });
  await page.locator('.wizard-choice-btn', { hasText: 'No, already installed' }).click();
  await page.locator('#wizard-next-btn').click();
  expect(await page.evaluate(() => boardFlashMode[1])).toBe('erase');
  await page.waitForFunction(() => wizardState.connectMode === 'seq');
  await page.evaluate(() => { closeWizard(); });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wiz_fake_save_steps saving a setup-wizard step reads its inputs: a claimed port\'s label left as its owner\'s name is stored blank, a renamed one kept; a port a Maestro leaves goes back to 9600 unless its baud was changed; the ETM numbers (an emptied one falling back to its default) and checksum; the Kyber ports; the NaviCore id', async ({ page }) => {
  // wizardSaveStep (app.js:11854-11994), wizardRemoveMaestro (11838-11851). Removing a Maestro row left its port at
  // the Maestro's baud (only a moved one was put back to 9600), and an emptied ETM field was saved as NaN and pushed
  // as such: both fixed 2026-10-12. Row wiz.save_steps.
  await setup(page);
  await openSetup(page);
  const go = (key) => page.evaluate((key) => {
    wizardState.steps = buildWizardSteps();
    wizardState.currentIdx = wizardState.steps.indexOf(key);
    wizardRenderStep();
  }, key);
  await page.evaluate(() => {
    Object.assign(wizardState, { controlSystem: 'other', maestroEnabled: true, quantity: 1 });
    wizardInitBoards(1);
    wizardState.maestros = [{ boardSlot: 1, id: 1, port: 1, baud: 57600 }, { boardSlot: 1, id: 2, port: 2, baud: 38400 }];
  });

  // Serial: the Maestro ports are pre-filled with their owner's name; left so, stored blank; renamed, kept.
  await go('serial');
  expect(await page.evaluate(() => [document.getElementById('wiz-b0-s0-label').value, document.getElementById('wiz-b0-s0-baud').value]))
    .toEqual(['Maestro ID 1', '57600']);
  await page.evaluate(() => {
    document.getElementById('wiz-b0-s1-label').value = 'Dome Maestro';
    document.getElementById('wiz-b0-s4-label').value = 'Lights';
    document.getElementById('wiz-b0-s4-baud').value = '19200';
    wizardSaveStep('serial');
  });
  expect(await page.evaluate(() => wizardState.boards[0].serialPorts.map((p) => [p.baud, p.label])))
    .toEqual([[57600, ''], [38400, 'Dome Maestro'], [9600, ''], [9600, ''], [19200, 'Lights']]);

  // Maestro config: Maestro 1 removed - its S1 back to 9600; Maestro 2 moved to S4 - its S2 back to 9600 too; a port
  // whose baud the user changed in between is left alone.
  await go('maestro-config');
  await page.evaluate(() => wizardRemoveMaestro(0));
  expect(await page.evaluate(() => [wizardState.maestros.length, wizardState.boards[0].serialPorts[0].baud])).toEqual([1, 9600]);
  await page.evaluate(() => { wizardState.boards[0].serialPorts[1].baud = 38400; document.getElementById('wiz-m0-port').value = '4'; wizardSaveStep('maestro-config'); });
  expect(await page.evaluate(() => [wizardState.maestros, wizardState.boards[0].serialPorts[1].baud]))
    .toEqual([[{ boardSlot: 1, id: 2, port: 4, baud: 38400 }], 9600]);
  await page.evaluate(() => {
    wizardState.boards[0].serialPorts[3].baud = 115200;     // changed by the user since
    document.getElementById('wiz-m0-port').value = '3';
    wizardSaveStep('maestro-config');
  });
  expect(await page.evaluate(() => wizardState.boards[0].serialPorts[3].baud)).toBe(115200);

  // ETM: the numbers and the checksum; an emptied number falls back to its default instead of NaN.
  await go('etm');
  await page.evaluate(() => {
    document.getElementById('wiz-etm-timeout').value = '';
    document.getElementById('wiz-etm-hb').value = '15';
    document.getElementById('wiz-etm-miss').value = '7';
    document.getElementById('wiz-etm-boot').value = '';
    document.getElementById('wiz-etm-chksm').checked = false;
    wizardSaveStep('etm');
  });
  expect(await page.evaluate(() => ({ ...wizardState.etmConfig }))).toEqual({ timeoutMs: 500, heartbeatSec: 15, missedHeartbeats: 7,
    bootHeartbeatSec: 2, messageCount: 20, messageDelayMs: 100, checksumEnabled: false });
  // ETM off: the numbers are left as they were, the checksum still read.
  await page.evaluate(() => {
    document.getElementById('wiz-etm-enabled').checked = false;
    document.getElementById('wiz-etm-hb').value = '99';
    document.getElementById('wiz-etm-chksm').checked = true;
    wizardSaveStep('etm');
  });
  expect(await page.evaluate(() => [wizardState.etmEnabled, wizardState.etmConfig.heartbeatSec, wizardState.etmConfig.checksumEnabled])).toEqual([false, 15, true]);

  // Kyber: board, port, baud, Marcduino port (None is no port).
  await page.evaluate(() => { Object.assign(wizardState, { controlSystem: 'kyber', kyberEnabled: true, quantity: 2 }); wizardInitBoards(2); });
  await go('kyber-config');
  await page.evaluate(() => {
    const set = (id, v) => { document.getElementById(id).value = v; };
    set('wiz-kyber-board', '2'); set('wiz-kyber-port', '1'); set('wiz-kyber-baud', '57600'); set('wiz-kyber-marcduino-port', '0');
    wizardSaveStep('kyber-config');
  });
  expect(await page.evaluate(() => [wizardState.kyberBoard, wizardState.kyberPort, wizardState.kyberBaud, wizardState.kyberMarcduinoPort]))
    .toEqual([2, 1, 57600, null]);
  // NaviCore: its id.
  await page.evaluate(() => { Object.assign(wizardState, { controlSystem: 'navicore', navicoreEnabled: true, kyberEnabled: false }); });
  await go('navicore-config');
  await page.evaluate(() => { document.getElementById('wiz-navicore-id').value = '15'; wizardSaveStep('navicore-config'); });
  expect(await page.evaluate(() => wizardState.navicoreId)).toBe(15);
  await page.evaluate(() => { closeWizard(); });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wiz_fake_apply_config Apply writes the setup wizard\'s state onto the config page: the quantity and General fields, NaviCore on every WCB slot with each board that has a Maestro listening remote, the whole Maestro table on every board, a client slot committed as a client, ETM off, and the controller and its id', async ({ page }) => {
  // wizardApplyConfig (app.js:12100-12272). The Kyber path is wizard.app_fake_setup_wizard's. Row wiz.apply_config.
  await setup(page);
  await openSetup(page);
  const m = await toastMark(page);
  await page.evaluate(() => {
    Object.assign(wizardState, {
      quantity: 3, password: 'dummy_wizard_mesh_pass', mac2: '1A', mac3: '2B', delimiter: '^', funcChar: '?', cmdChar: ';',
      controlSystem: 'navicore', navicoreEnabled: true, useSpecialPeer: true, navicoreId: 17, kyberEnabled: false,
      maestroEnabled: true, maestros: [{ boardSlot: 2, id: 1, port: 1, baud: 57600 }], etmEnabled: false,
    });
    wizardInitBoards(3);
    wizardState.boards[0].hwVersion = 24;
    wizardState.boards[1].hwVersion = 32;
    wizardState.boards[1].serialPorts[1] = { baud: 38400, label: 'Panel' };
    Object.assign(wizardState.boards[2], { type: 'client', clientAlias: 'Dome Sensor', wcbNumber: 3 });
    wizardApplyConfig();
  });
  expect(await toastsSince(page, m)).toContain('Config page updated from wizard');
  const r = await page.evaluate(() => {
    const b = (n) => boardConfigs[n];
    return {
      general: ['g-wcbq', 'g-password', 'g-mac2', 'g-mac3', 'g-navicore-id'].map((id) => document.getElementById(id).value),
      sys: [systemConfig.general.wcbQuantity, systemConfig.general.controller, systemConfig.general.specialPeer, systemConfig.general.specialPeerId, systemConfig.general.etm.enabled],
      etmBox: document.getElementById('g-etm-enabled').checked,
      modes: [1, 2].map((n) => [b(n).kyber.mode, b(n).specialPeer, b(n).specialPeerId, b(n).etm.enabled, b(n).hwVersion]),
      maestros: [b(1).maestros, b(2).maestros], tables: [b(1).maestroTable, b(2).maestroTable],
      s2: [b(2).serialPorts[1].baud, b(2).serialPorts[1].label],
      client: [b(3).type, b(3).clientAlias, b(3).wcbNumber, b(3).specialPeer, document.getElementById('b3-client-pane').style.display !== 'none'],
      sections: [...document.querySelectorAll('#boards-container [id^="section-board-"]')].map((el) => +el.id.replace('section-board-', '')),
    };
  });
  const T = [{ id: 1, wcb: 2, port: 1, baud: 57600 }];
  expect(r).toEqual({
    general: ['3', 'dummy_wizard_mesh_pass', '1A', '2B', '17'],
    sys: [3, 'navicore', true, 17, false],
    etmBox: false,
    modes: [['none', true, 17, false, 24], ['remote', true, 17, false, 32]],
    maestros: [[], [{ id: 1, port: 1, baud: 57600 }]], tables: [T, T],
    s2: [38400, 'Panel'],
    client: ['client', 'Dome Sensor', 3, false, true],
    sections: [1, 2, 3],
  });
  // What a full push of each WCB would then carry.
  const chain = (n) => page.evaluate((n) => WCBParser.buildCommandString(boardConfigs[n], null, true).split('^'), n);
  expect(await chain(1)).toEqual(expect.arrayContaining(['?CONTROLLER,ON,17', '?ETM,OFF', '?MAESTRO,M1:W2S1:57600', '?MAC,2,1A']));
  expect(await chain(2)).toEqual(expect.arrayContaining(['?MAESTRO,REMOTE', '?CONTROLLER,ON,17', '?MAESTRO,M1:W2S1:57600']));
  await page.evaluate(() => { closeWizard(); });
  expect(page.wizErrors).toEqual([]);
});
