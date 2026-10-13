// Needs no board: the setup wizard (Wizard/app.js) - opening and closing it, its step list, the checks each step makes,
// what each step renders, the connect watcher's time-out and the progress it mirrors - the low-risk gaps the 2026-10-12
// coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json,
// named at the end of each test's comment). The wizard runs in the real page; nothing is connected. Runs standalone,
// in CI, and under the harness by id (s30_wizard.py, _FAKE_RESCAN_L).
//
// The page's openWizard() is the setup wizard; lib/wizard.js's openWizard (inside setup) loads the page. Nothing here
// is a real credential: the wizard's network password is the one it generates, or a dummy.
const { test, expect } = require('../lib/fixtures');
const { setup, toastMark, toastsSince } = require('../lib/rescan');

// The wizard's state for a two-WCB Kyber system (WCB1 hosts the Kyber on S2, its Marcduino port on S3, a Maestro on S1;
// WCB2 is a HW 3.2 board called Dome with a Maestro on S4), on the given step, rendered.
async function kyberState(page, step, extra = {}) {
  await page.evaluate(({ step, extra }) => {
    openWizard();
    Object.assign(wizardState, {
      quantity: 2, password: 'dummyWizardPw23', mac2: '1a', mac3: '2B', controlSystem: 'kyber', kyberEnabled: true,
      kyberBoard: 1, kyberPort: 2, kyberBaud: 115200, kyberMarcduinoPort: 3, maestroEnabled: true,
      maestros: [{ boardSlot: 1, id: 1, port: 1, baud: 57600 }, { boardSlot: 2, id: 2, port: 4, baud: 57600 }],
    }, extra);
    wizardState.boards = [Object.assign(wizardDefaultBoard(1), { hwVersion: 24 }),
                          Object.assign(wizardDefaultBoard(2), { hwVersion: 32, alias: 'Dome' })];
    wizardState.steps = buildWizardSteps();
    wizardState.currentIdx = wizardState.steps.indexOf(step);
    wizardRenderStep();
  }, { step, extra });
}
const opts = (page, id) => page.evaluate((id) => [...document.getElementById(id).options]
  .map((o) => `${o.value}${o.disabled ? ' (off)' : ''}${o.selected ? ' *' : ''}`), id);
const shownEl = (page, id) => page.evaluate((id) => {
  const el = document.getElementById(id);
  return !!el && el.style.display !== 'none' && el.offsetParent !== null;
}, id);
const status = (page, n) => page.evaluate((n) => {
  const el = document.getElementById(`wiz-connect-status-${n}`);
  return [el.className, el.textContent];
}, n);

test('wizard.wiz_fake_open_steps opening the setup wizard starts a fresh session - a random 14-character password from the unambiguous set and two random hex MAC octets - with the step list its answers call for (NaviCore or Kyber setup after the control choice, Maestro setup after a yes); closing it stops every connect watcher and port-detect job', async ({ page }) => {
  // openWizard, closeWizard, _wizardClearAllWatchers, wizardGenPassword, wizardGenMacOctet, buildWizardSteps
  // (app.js:10520-10560, 12091-12097). Row wiz.open_steps.
  await setup(page);
  const open = () => page.evaluate(() => {
    openWizard();
    return { pw: wizardState.password, mac2: wizardState.mac2, mac3: wizardState.mac3, idx: wizardState.currentIdx,
             steps: wizardState.steps, open: _wizardOpen, modal: document.getElementById('wizard-modal').classList.contains('open'),
             session: _wizardSessionId };
  });
  const a = await open();
  expect(a.pw).toMatch(/^[ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789]{14}$/);
  expect(a.mac2).toMatch(/^[0-9A-F]{2}$/);
  expect(a.mac3).toMatch(/^[0-9A-F]{2}$/);
  expect([a.idx, a.open, a.modal]).toEqual([0, true, true]);
  expect(a.steps).toEqual(['welcome', 'quantity', 'identity', 'control', 'maestro', 'serial', 'network', 'etm', 'review', 'firmware', 'connect']);
  const b = await open();
  expect(b.session).toBe(a.session + 1);
  expect(b.pw, 'a new password each time').not.toBe(a.pw);

  const steps = (cs, maestro) => page.evaluate(({ cs, maestro }) => {
    wizardState.controlSystem = cs;
    wizardState.maestroEnabled = maestro;
    return buildWizardSteps().join(' ');
  }, { cs, maestro });
  expect(await steps('navicore', false)).toBe('welcome quantity identity control navicore-config maestro serial network etm review firmware connect');
  expect(await steps('kyber', true)).toBe('welcome quantity identity control kyber-config maestro maestro-config serial network etm review firmware connect');
  expect(await steps('other', true)).toBe('welcome quantity identity control maestro maestro-config serial network etm review firmware connect');

  // Closing: watchers cleared, detect jobs cancelled, snapshots dropped. A click inside the dialog is not a close.
  const closed = await page.evaluate(() => {
    window.__cancelled = [];
    wizardConnectWatchers[1] = setInterval(() => { window.__ticked = true; }, 50);
    _wizPortDetectJobs[2] = { cancel: () => window.__cancelled.push(2) };
    _wizPortConfigSnaps[2] = { configSnapshot: null };
    closeWizard({ target: document.getElementById('wizard-body') });
    const stillOpen = _wizardOpen;
    closeWizard();
    window.__ticked = false;
    return { stillOpen, open: _wizardOpen, modal: document.getElementById('wizard-modal').classList.contains('open'),
             watchers: Object.keys(wizardConnectWatchers), jobs: Object.keys(_wizPortDetectJobs), snaps: Object.keys(_wizPortConfigSnaps),
             cancelled: window.__cancelled };
  });
  expect(closed).toEqual({ stillOpen: true, open: false, modal: false, watchers: [], jobs: [], snaps: [], cancelled: [2] });
  await page.waitForTimeout(200);
  expect(await page.evaluate(() => window.__ticked), 'the watcher no longer runs').toBe(false);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wiz_fake_validate_steps the wizard\'s checks: a control system must be chosen; the network password needs 8 characters and each MAC octet two hex digits; slot ids must be 1-99; two Maestros on one board and port are refused; Next with a failing check toasts it and stays put', async ({ page }) => {
  // wizardValidateStep (app.js:11997-12088), wizardNext (10562-10575). Unique ids, the controller's reserved id, the HW
  // version and the Kyber-port clashes are wizard.app_fake_setup_wizard's; the network characters
  // wizard.push_fake_delimiter's. Row wiz.validate_steps.
  await setup(page);
  const check = (key) => page.evaluate((key) => wizardValidateStep(key), key);
  await page.evaluate(() => openWizard());
  expect(await check('control')).toBe('Please choose a control system — NaviCore, Kyber, or Other.');
  await page.evaluate(() => { wizardState.controlSystem = 'other'; });
  expect(await check('control')).toBe(null);

  // The network step, as rendered.
  await page.evaluate(() => { wizardState.currentIdx = wizardState.steps.indexOf('network'); wizardRenderStep(); });
  const net = (pw, m2, m3) => page.evaluate(({ pw, m2, m3 }) => {
    document.getElementById('wiz-password').value = pw;
    document.getElementById('wiz-mac2').value = m2;
    document.getElementById('wiz-mac3').value = m3;
    return wizardValidateStep('network');
  }, { pw, m2, m3 });
  expect(await net('short77', '1A', '2B')).toBe('Password must be at least 8 characters.');
  expect(await net('  short77  ', '1A', '2B'), 'spaces round it do not count').toBe('Password must be at least 8 characters.');
  expect(await net('dummy_pw8', 'ZZ', '2B')).toBe('MAC Octet 2 must be two hex digits (00–FF).');
  expect(await net('dummy_pw8', 'A', '2B')).toBe('MAC Octet 2 must be two hex digits (00–FF).');
  expect(await net('dummy_pw8', '1a', 'G1')).toBe('MAC Octet 3 must be two hex digits (00–FF).');
  expect(await net('dummy_pw8', '1a', '2b')).toBe(null);
  // Next on a failing step: the toast, and the same step.
  await page.evaluate(() => { document.getElementById('wiz-password').value = 'short'; });
  const m = await toastMark(page);
  await page.evaluate(() => wizardNext());
  expect(await toastsSince(page, m)).toBe('Password must be at least 8 characters.');
  expect(await page.evaluate(() => wizardState.steps[wizardState.currentIdx])).toBe('network');

  // Identity: an id out of 1-99.
  await page.evaluate(() => {
    wizardState.quantity = 2;
    wizardInitBoards(2);
    wizardState.boards.forEach((b) => { b.hwVersion = 24; });
    wizardState.currentIdx = wizardState.steps.indexOf('identity');
    wizardRenderStep();
  });
  const ids = (a, b) => page.evaluate(({ a, b }) => {
    document.getElementById('wiz-b0-wcbnum').value = a;
    document.getElementById('wiz-b1-wcbnum').value = b;
    return wizardValidateStep('identity');
  }, { a, b });
  for (const [a, b] of [['0', '2'], ['1', '100'], ['', '2'], ['-3', '2']]) {
    expect.soft(await ids(a, b), `${a},${b}`).toBe('Each slot must have an ID between 1 and 99.');
  }
  expect(await ids('1', '99')).toBe(null);

  // Maestro setup: two rows on Board 1, Serial 1.
  await page.evaluate(() => {
    wizardState.maestroEnabled = true;
    wizardState.steps = buildWizardSteps();
    wizardState.maestros = [{ boardSlot: 1, id: 1, port: 1, baud: 57600 }, { boardSlot: 1, id: 2, port: 1, baud: 57600 }];
    wizardState.currentIdx = wizardState.steps.indexOf('maestro-config');
    wizardRenderStep();
  });
  expect(await check('maestro-config'))
    .toBe('Two Maestros are both assigned to Board 1, Serial 1. Each Maestro must use a unique port.');
  // The live picker counts: the second moved to S2 on screen, the check passes.
  await page.evaluate(() => { document.getElementById('wiz-m1-port').value = '2'; });
  expect(await check('maestro-config')).toBe(null);
  // Same port on different boards is fine.
  await page.evaluate(() => { document.getElementById('wiz-m1-port').value = '1'; document.getElementById('wiz-m1-board').value = '2'; });
  expect(await check('maestro-config')).toBe(null);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wiz_fake_step_html each wizard step renders its controls from the wizard\'s state: identity tabs with the WCB or client fields and the HW 3.2 USB note; serial rows with the Kyber and Maestro ports locked at their bauds; Kyber S1/S2 with a Marcduino list leaving out the Kyber port; Maestro ports with the Kyber ones disabled and no client board; ETM fields; the review summary; the firmware choice; the connect rows', async ({ page }) => {
  // wizardHTMLIdentity, wizardHTMLSerial, wizardHTMLKyberConfig, onWizKyberPortChange, wizardHTMLMaestroConfig,
  // wizardHTMLEtm, wizardHTMLReview, wizardHTMLFirmware, wizardHTMLConnect (app.js:10877-11590). The identity and
  // maestro-config renders are also wizard.app_fake_setup_wizard's. Row wiz.step_html.
  await setup(page);

  // Identity: a tab per slot; WCB2 (HW 3.2) shows its LED pin and the two-USB-port note, WCB1 (2.4) neither.
  await kyberState(page, 'identity');
  expect(await page.evaluate(() => [...document.querySelectorAll('.wizard-board-tab')].map((t) => t.textContent.trim().replace(/\s/g, ' '))))
    .toEqual(['WCB 1', 'WCB 2']);
  expect(await page.evaluate(() => ['wiz-b0-led-row', 'wiz-b0-s3usb-note', 'wiz-b1-led-row', 'wiz-b1-s3usb-note', 'wiz-b1-client-fields']
    .map((id) => document.getElementById(id).style.display !== 'none'))).toEqual([false, false, true, true, false]);
  expect(await page.evaluate(() => document.getElementById('wiz-b1-s3usb-note').textContent)).toContain('HW 3.2 (ESP32-S3) has two USB ports');
  expect(await page.evaluate(() => document.getElementById('wiz-b1-alias').value)).toBe('Dome');
  // Flipped to a client: its client fields instead, with the same id.
  await page.evaluate(() => {
    document.querySelector('input[name="wiz-slot-type-1"][value="client"]').checked = true;
    wizardOnSlotTypeChange(1);
  });
  expect(await page.evaluate(() => [document.getElementById('wiz-b1-wcb-fields').style.display, document.getElementById('wiz-b1-client-fields').style.display,
                                    document.getElementById('wiz-b1-cnum').value, wizardState.boards[1].type]))
    .toEqual(['none', '', '2', 'client']);

  // Serial: WCB1's claimed ports locked at their bauds with the owner named; S3-S5 free ports offer up to 57600.
  await kyberState(page, 'serial');
  const serialRows = (i) => page.evaluate((i) => [...document.querySelectorAll(`#wiz-panel-serial-${i} tbody tr`)].map((tr) => {
    const sel = tr.querySelector('select');
    return sel ? `editable ${[...sel.options].at(-1).value}` : tr.textContent.replace(/\s+/g, ' ').trim();
  }), i);
  expect(await serialRows(0)).toEqual(['Serial 1 57,600 (locked) 🔒 Maestro ID 1', 'Serial 2 115,200 (locked) 🔒 Kyber Maestro',
                                       'Serial 3 9,600 (locked) 🔒 Kyber Marcduino', 'editable 57600', 'editable 57600']);
  expect(await page.evaluate(() => ['wiz-b0-s0-label', 'wiz-b0-s1-label', 'wiz-b0-s2-label'].map((id) => document.getElementById(id).value)))
    .toEqual(['Maestro ID 1', 'Kyber Maestro', 'Kyber Marcduino']);
  expect(await serialRows(1)).toEqual(['editable 256000', 'editable 256000', 'editable 57600', 'Serial 4 57,600 (locked) 🔒 Maestro ID 2',
                                       'editable 57600']);

  // Kyber: WCB slots only, S1/S2 only, the Marcduino list without the Kyber port; it follows a Kyber port change.
  await kyberState(page, 'kyber-config');
  expect(await opts(page, 'wiz-kyber-board')).toEqual(['1 *', '2']);
  expect(await opts(page, 'wiz-kyber-port')).toEqual(['1', '2 *']);
  expect(await opts(page, 'wiz-kyber-baud')).toEqual(['9600', '38400', '57600', '115200 *']);
  expect(await opts(page, 'wiz-kyber-marcduino-port')).toEqual(['0', '1', '3 *', '4', '5']);
  await page.evaluate(() => { document.getElementById('wiz-kyber-port').value = '1'; onWizKyberPortChange(); });
  expect(await opts(page, 'wiz-kyber-marcduino-port')).toEqual(['0', '2', '3 *', '4', '5']);
  // WCB2 a client: not offered.
  await kyberState(page, 'kyber-config');
  await page.evaluate(() => { wizardState.boards[1].type = 'client'; document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('kyber-config'); });
  expect(await opts(page, 'wiz-kyber-board')).toEqual(['1 *']);

  // Maestro setup: on the Kyber board its two ports are listed but disabled; WCB2's row offers all five; a client
  // board is not offered; bauds on S4 stop at 57600.
  await kyberState(page, 'maestro-config');
  expect(await opts(page, 'wiz-m0-port')).toEqual(['1 *', '2 (off)', '3 (off)', '4', '5']);
  expect(await opts(page, 'wiz-m1-port')).toEqual(['1', '2', '3', '4 *', '5']);
  expect((await opts(page, 'wiz-m1-baud')).at(-1)).toBe('57600 *');
  expect(await opts(page, 'wiz-m0-id')).toEqual(['1 *', '2', '3', '4', '5', '6', '7', '8', '9']);
  expect(await opts(page, 'wiz-m0-board')).toEqual(['1 *', '2']);
  await page.evaluate(() => { wizardState.boards[1].type = 'client'; document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('maestro-config'); });
  expect(await opts(page, 'wiz-m1-board'), 'the client slot\'s Maestro moved to a WCB slot').toEqual(['1 *']);

  // ETM: the fields from the state, hidden when ETM is unticked.
  await kyberState(page, 'etm', { etmConfig: { timeoutMs: 650, heartbeatSec: 12, missedHeartbeats: 4, bootHeartbeatSec: 3,
                                               messageCount: 20, messageDelayMs: 100, checksumEnabled: false } });
  expect(await page.evaluate(() => ['wiz-etm-timeout', 'wiz-etm-hb', 'wiz-etm-miss', 'wiz-etm-boot'].map((id) => document.getElementById(id).value)
    .concat([document.getElementById('wiz-etm-chksm').checked, document.getElementById('wiz-etm-enabled').checked])))
    .toEqual(['650', '12', '4', '3', false, true]);
  expect(await shownEl(page, 'wiz-etm-fields')).toBe(true);
  await page.evaluate(() => wizardToggleEtmDetail(false));
  expect([await shownEl(page, 'wiz-etm-fields'), await page.evaluate(() => wizardState.etmEnabled)]).toEqual([false, false]);

  // Review: the system, each slot, the optional features; NaviCore only when it is on.
  await kyberState(page, 'review');
  const review = () => page.evaluate(() => [...document.querySelectorAll('#wizard-body .wizard-review-row')]
    .map((r) => `${r.querySelector('.wizard-review-label').textContent}: ${r.querySelector('.wizard-review-value').textContent}`));
  expect(await review()).toEqual(['Slots total: 2 (2 WCBs, 0 Clients)', 'ESP-NOW Password: dummyWizardPw23', 'MAC Octets: XX:XX:1A:2B:XX:XX',
                                  'Slot 1: WCB 1 · HW 2.4', 'Slot 2: WCB 2 · HW 3.2 · Dome',
                                  'Kyber: ✅ Enabled', 'Maestro: ✅ Enabled', 'ETM: ✅ Enabled']);
  expect(await page.evaluate(() => [document.getElementById('wizard-next-btn').textContent, !!document.getElementById('wizard-export-btn')]))
    .toEqual(['Apply & Connect →', true]);
  await kyberState(page, 'review', { kyberEnabled: false, controlSystem: 'navicore', navicoreEnabled: true, navicoreId: 17, maestroEnabled: false,
                                     etmEnabled: false });
  await page.evaluate(() => { wizardState.boards[1].type = 'client'; wizardState.boards[1].clientAlias = 'Probe';
                              document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('review'); });
  expect(await review()).toEqual(['Slots total: 2 (1 WCB, 1 Client)', 'ESP-NOW Password: dummyWizardPw23', 'MAC Octets: XX:XX:1A:2B:XX:XX',
                                  'NaviCore: ✅ Enabled · peer ID 17', 'Slot 1: WCB 1 · HW 2.4', 'Slot 2: Client · ID 2 · Probe',
                                  'Kyber: —', 'Maestro: —', 'ETM: —']);

  // Firmware: "No, already installed" until Yes is picked.
  await kyberState(page, 'firmware');
  const fw = () => page.evaluate(() => [...document.querySelectorAll('#wizard-body .wizard-choice-btn')]
    .map((b) => `${b.querySelector('.wizard-choice-label').textContent}:${b.className.replace('wizard-choice-btn', '').trim()}`));
  expect(await fw()).toEqual(['Yes, upload firmware:', 'No, already installed:selected-no']);
  await page.evaluate(() => wizardFirmwareChoice(true));
  expect(await fw()).toEqual(['Yes, upload firmware:selected', 'No, already installed:']);

  // Connect: one row per WCB slot - the first active, the next "Up next" - with the flash badge; a client slot listed
  // below instead; the HW 3.2 port guidance while a 3.2 board is in the system.
  await kyberState(page, 'firmware', { connectMode: 'seq', connectSeqN: 1, needsFirmware: true, eraseNvs: true });
  await page.evaluate(() => {
    wizardState.quantity = 3;
    wizardState.boards.push(Object.assign(wizardDefaultBoard(3), { type: 'client', clientAlias: 'Probe' }));
    document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('connect');
  });
  const rows = await page.evaluate(() => [1, 2, 3].map((n) => {
    const r = document.getElementById(`wiz-connect-row-${n}`);
    return r && { dimmed: r.classList.contains('wiz-connect-dimmed'), status: document.getElementById(`wiz-connect-status-${n}`).textContent,
                  badge: r.querySelector('.badge')?.textContent ?? null,
                  next: document.getElementById(`wiz-connect-pending-${n}`).style.display !== 'none' };
  }));
  expect(rows).toEqual([{ dimmed: false, status: 'Select port…', badge: 'Factory Reset', next: false },
                        { dimmed: true, status: '', badge: 'Factory Reset', next: true }, null]);
  const body = await page.evaluate(() => document.getElementById('wizard-body').textContent.replace(/\s+/g, ' '));
  expect(body).toContain('Client slots — not connected by the Wizard');
  expect(body).toContain('ID 3 — Probe');
  expect(body).toContain('HW 3.2 (ESP32-S3) has two USB ports');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wiz_fake_watch_timeout the setup wizard\'s connect watcher checks every 500 ms and gives up after 90 s (the 181st check) with no board connected: "Timed out", the watcher dropped and the row\'s Reconnect button back', async ({ page }) => {
  // wizardWatchForConnect's time-out branch (app.js:13008-13013). The rest of the watcher is
  // wizard.wiz_fake_watch_push_verify's. Row wiz.watch_push_verify (its remaining branch).
  await setup(page);
  await kyberState(page, 'firmware', { connectMode: 'seq', connectSeqN: 1 });
  await page.evaluate(() => { document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('connect'); });
  // The watcher's interval, captured: the spec runs its checks itself.
  const ms = await page.evaluate(() => {
    const real = window.setInterval;
    let ms = null;
    window.setInterval = (fn, t) => { window.__check = fn; ms = t; return real(() => {}, 1e9); };
    try { wizardWatchForConnect(1, null, null); } finally { window.setInterval = real; }
    return ms;
  });
  expect(ms).toBe(500);
  expect(await page.evaluate(() => !!wizardConnectWatchers[1])).toBe(true);
  const before = await status(page, 1);
  await page.evaluate(async () => { for (let i = 0; i < 180; i++) await window.__check(); });
  expect(await status(page, 1), 'still waiting after 180 checks (90 s)').toEqual(before);
  expect(await page.evaluate(() => !!wizardConnectWatchers[1])).toBe(true);
  await page.evaluate(() => window.__check());
  expect(await status(page, 1)).toEqual(['wizard-connect-status err', '✕ Timed out']);
  expect(await page.evaluate(() => [!!wizardConnectWatchers[1], document.getElementById('wiz-connect-btns-1').style.display,
                                    document.getElementById('wiz-cancel-btn-1').style.display])).toEqual([false, 'flex', 'none']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wiz_fake_status_mirror while the setup wizard\'s connect row is on screen, a board\'s push and flash progress is mirrored into it ("Pushing config... 50%", "Flashing firmware... 30%"); with no row, only the card shows it', async ({ page }) => {
  // mirrorStatusToWizard, reportPushProgress, updateFlashBar (app.js:10263-10296). Row wiz.status_mirror.
  await setup(page);
  await kyberState(page, 'firmware', { connectMode: 'seq', connectSeqN: 1 });
  await page.evaluate(() => { document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('connect'); });
  await page.evaluate(() => reportPushProgress(1, 1, 2));
  expect(await status(page, 1)).toEqual(['wizard-connect-status busy', '📤 Pushing config… 50%']);
  expect(await page.evaluate(() => document.getElementById('b1-flash-status').textContent)).toBe('Pushing… 1/2 settings (50%)');
  await page.evaluate(() => updateFlashBar(1, 30, 100));
  expect(await status(page, 1)).toEqual(['wizard-connect-status busy', '⚡ Flashing firmware… 30%']);
  expect(await page.evaluate(() => document.getElementById('b1-flash-status').textContent)).toBe('Flashing… 30%');
  // WCB2's row: its own.
  await page.evaluate(() => reportPushProgress(2, 2, 2));
  expect(await status(page, 2)).toEqual(['wizard-connect-status busy', '📤 Pushing config… 100%']);
  expect((await status(page, 1))[1]).toBe('⚡ Flashing firmware… 30%');
  // No connect row on screen: the card alone.
  await page.evaluate(() => { closeWizard(); document.getElementById('wizard-body').innerHTML = ''; reportPushProgress(1, 2, 2); });
  expect(await page.evaluate(() => [document.getElementById('wiz-connect-status-1'), document.getElementById('b1-flash-status').textContent]))
    .toEqual([null, 'Pushing… 2/2 settings (100%)']);
  expect(page.wizErrors).toEqual([]);
});
