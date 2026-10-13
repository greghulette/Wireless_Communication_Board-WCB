// Needs no board: the General section (Wizard/app.js) - the medium-risk gaps the 2026-10-12 coverage re-scan found
// (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each
// test's comment): the quantity, the network fields, ETM, and the keep/use modal a differing board opens. Fake boards
// in the real page (lib/fake.js): boardPull and boardGo run as on the bench, the handlers are reached through the
// inputs' own events, and only the wire is recorded. Runs standalone, in CI, and under the harness by id
// (s30_wizard.py). Verify pulls are held back where a spec pushes more than once (lib/rescan.js holdVerifyPulls).
//
// Nothing here is a real credential: the mesh passwords are dummies.
const { test, expect } = require('../lib/fixtures');
const { board, pullFake, push, edit } = require('../lib/fake');
const { setup, holdVerifyPulls, pull, toastMark, toastsSince, options, shown, sent } = require('../lib/rescan');
const pushAllPending = (page) => page.evaluate(() => document.getElementById('btn-push-all').classList.contains('btn-pending'));
const sections = (page) => page.evaluate(() => [...document.querySelectorAll('#boards-container [id^="section-board-"]')]
  .map((el) => +el.id.replace('section-board-', '')));

test('wizard.general_fake_wcbq_change WCB Quantity: More... lists 1-20, a pick sets the floor - the grid grows to it and shrinks back, keeping a connected board above it - widens every board\'s WCB# dropdown to the quantity or its own number, moves the General baseline and marks General pending', async ({ page }) => {
  // onWCBQuantityChange (app.js:1280-1303) with expandWCBDropdown / populateWCBDropdown (1250-1277) and renderBoards
  // (524-527). Every other General handler marked General pending; this one did not, so a quantity change left Push All
  // green while every board's next push carried ?WCBQ - fixed 2026-10-12 (_notifyGeneralChanged). Row general.wcbq_change.
  await setup(page);
  await pullFake(page, 1, board());
  await pullFake(page, 5, board([], { wcb: 5 }));          // WCB5 on USB, above the quantity of 2
  await holdVerifyPulls(page);
  expect(await sections(page)).toEqual([1, 2, 5]);
  expect(await options(page, 'g-wcbq')).toEqual(['1', '2', '3', '4', '5', '6', '_more']);
  expect(await pushAllPending(page)).toBe(false);

  // More...: the whole 1-20 list, the quantity unchanged until a number is picked.
  await edit(page, '#g-wcbq', '_more');
  expect(await options(page, 'g-wcbq')).toEqual(Array.from({ length: 20 }, (_, i) => String(i + 1)));
  expect(await page.evaluate(() => [document.getElementById('g-wcbq').value, systemConfig.general.wcbQuantity])).toEqual(['2', 2]);
  expect(await sections(page)).toEqual([1, 2, 5]);

  // 9: the floor, and every board's WCB# dropdown offers 1-9.
  let m = await toastMark(page);
  await edit(page, '#g-wcbq', '9');
  expect(await sections(page)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
  const nine = Array.from({ length: 9 }, (_, i) => String(i + 1));
  expect(await options(page, 'b1-wcb-number')).toEqual(nine);
  expect(await options(page, 'b5-wcb-number')).toEqual(nine);
  expect(await page.evaluate(() => [document.getElementById('b5-wcb-number').value, systemConfig.general.wcbQuantity,
                                    generalBaseline.fields.wcbQuantity])).toEqual(['5', 9, 9]);
  expect(await pushAllPending(page), 'General pending: every board\'s push now carries the quantity').toBe(true);
  expect(await toastsSince(page, m)).toContain('Changes pending — push to all boards to apply');
  expect(await sent(page, 1)).toEqual(['1:?WCBQ,9']);
  expect(await sent(page, 5)).toEqual(['5:?WCBQ,9']);

  // Back to 2: the empty sections go with their configs; WCB5, connected, stays with its pulled config.
  await edit(page, '#g-wcbq', '2');
  expect(await sections(page)).toEqual([1, 2, 5]);
  expect(await page.evaluate(() => Object.keys(boardConfigs).map(Number).sort((a, b) => a - b))).toEqual([1, 2, 5]);
  expect(await page.evaluate(() => [boardConfigs[5].wcbNumber, boardBaselines[5].wcbNumber])).toEqual([5, 5]);
  expect(await options(page, 'b1-wcb-number')).toEqual(['1', '2']);
  // A board above the quantity keeps its own number on offer: the dropdown is never cut below it (W-18).
  expect(await options(page, 'b5-wcb-number')).toContain('5');
  expect(await sent(page, 1)).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.general_fake_password_mac_channel the password, MAC octets and mesh channel, edited in General, are written into every board\'s config and the General baseline, mark General pending (Push All amber), and every board\'s push then carries them', async ({ page }) => {
  // onGeneralPasswordChange, onGeneralMacChange, onMeshChannelChange (app.js:1305-1334) and _notifyGeneralChanged
  // (1232-1237), read back by boardGo (8191-8205). Row general.password_mac_channel.
  await setup(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([], { wcb: 2 }));
  await holdVerifyPulls(page);
  expect(await pushAllPending(page)).toBe(false);

  const m = await toastMark(page);
  await edit(page, '#g-meshch', '6');
  expect(await page.evaluate(() => [boardConfigs[1].meshChannel, boardConfigs[2].meshChannel, systemConfig.general.meshChannel,
                                    generalBaseline.fields.meshChannel, generalSettingsDirty])).toEqual([6, 6, 6, 6, true]);
  expect(await pushAllPending(page)).toBe(true);
  expect(await toastsSince(page, m)).toContain('Changes pending — push to all boards to apply');
  for (const n of [1, 2]) {
    const r = await push(page, n, { skipReboot: true });
    expect([r.sent, r.returned], `WCB ${n}: a channel change reboots`).toEqual([[`${n}:?WCBCH,6`], true]);
  }

  await edit(page, '#g-password', 'dummy_new_mesh_password');
  await edit(page, '#g-mac2', '7a');
  expect(await page.evaluate(() => [1, 2].map((n) => [boardConfigs[n].espnowPassword, boardConfigs[n].macOctet2, boardConfigs[n].macOctet3])))
    .toEqual([['dummy_new_mesh_password', '7A', '4B'], ['dummy_new_mesh_password', '7A', '4B']]);
  expect(await page.evaluate(() => [generalBaseline.fields.espnowPassword, generalBaseline.fields.macOctet2]))
    .toEqual(['dummy_new_mesh_password', '7A']);
  for (const n of [1, 2]) {
    expect(await sent(page, n)).toEqual([`${n}:?WCBCH,6`, `${n}:?MAC,2,7A`, `${n}:?EPASS,dummy_new_mesh_password`]);
  }
  // A board pulled with the new values is no mismatch: the baseline moved with the edits.
  await pull(page, 3, board(['WCBCH,6', 'MAC,2,7A', 'EPASS,dummy_new_mesh_password'], { wcb: 3, wcbq: 2 }));
  await page.waitForTimeout(400);
  expect(await page.locator('#general-conflict-modal.open').count()).toBe(0);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.general_fake_etm_toggle_checksum ETM Enabled, unchecked in General, turns ETM off on every board and the push sends ?ETM,OFF alone; checked on a board pulled with it off, ?ETM,ON alone; Packet Checksum unchecked is never pushed, and a board pulled with it off is put back ON', async ({ page }) => {
  // onETMToggle and onETMChecksumToggle (app.js:1406-1423) write every board's etm; buildCommandString's ETM block
  // (parser.js:1645-1659) sends ETM,ON/OFF on a change, the detail lines only when ETM is on and they differ from the
  // board's, and ETM,CHKSM,ON whenever the board's checksum is not on - never OFF. A board pulled with ETM off left the
  // General detail inputs at the page's defaults (missed heartbeats 3, not the firmware's 5), and the first ETM edit
  // pushed them over the board's: fixed 2026-10-12, syncGeneralFromConfig (9269-9279) shows the details either way and
  // index.html's default is 5. Row general.etm_toggle_checksum.
  await setup(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([], { wcb: 2 }));
  await holdVerifyPulls(page);
  await edit(page, '#g-etm-enabled', false);
  expect(await page.evaluate(() => [boardConfigs[1].etm.enabled, boardConfigs[2].etm.enabled, systemConfig.general.etm.enabled,
                                    generalBaseline.fields.etmEnabled, generalSettingsDirty])).toEqual([false, false, false, false, true]);
  expect(await sent(page, 1)).toEqual(['1:?ETM,OFF']);
  expect(await sent(page, 2)).toEqual(['2:?ETM,OFF']);
  // A detail edited while ETM is off goes nowhere: the firmware keeps its own until ETM is on again.
  await edit(page, '#g-etm-timeout', '700');
  expect(await sent(page, 1)).toEqual(['1:?ETM,OFF']);
  await edit(page, '#g-etm-timeout', '500');
  await edit(page, '#g-etm-enabled', true);
  expect(await page.evaluate(() => [boardConfigs[1].etm.enabled, boardConfigs[2].etm.enabled])).toEqual([true, true]);
  expect((await push(page, 1)).outcome.reason, 'checked again: the board\'s own state').toBe('no changes to push');

  // Pulled with ETM off: its details are shown all the same (the board keeps them); checked, the push sends ETM,ON
  // and no detail line, and a detail changed with it goes after it - the others stay the board's own.
  await setup(page);
  await pullFake(page, 1, board(['ETM,OFF', 'ETM,TIMEOUT,650']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => ['g-etm-enabled', 'g-etm-timeout', 'g-etm-miss'].map((id) => {
    const el = document.getElementById(id); return el.type === 'checkbox' ? el.checked : el.value; }))).toEqual([false, '650', '5']);
  await edit(page, '#g-etm-enabled', true);
  expect(await sent(page, 1)).toEqual(['1:?ETM,ON']);
  await edit(page, '#g-etm-hb', '12');
  expect(await sent(page, 1)).toEqual(['1:?ETM,ON', '1:?ETM,HB,12']);

  // Packet Checksum unchecked: every board's config follows, but nothing is ever pushed to turn it off.
  await setup(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([], { wcb: 2 }));
  await holdVerifyPulls(page);
  await edit(page, '#g-etm-chksm', false);
  expect(await page.evaluate(() => [boardConfigs[1].etm.checksumEnabled, boardConfigs[2].etm.checksumEnabled,
                                    generalBaseline.fields.etmChecksum, generalSettingsDirty])).toEqual([false, false, false, true]);
  expect((await push(page, 1)).outcome.reason).toBe('no changes to push');
  // A board pulled with the checksum off is corrected by its next push, edit or not.
  await setup(page);
  await pullFake(page, 1, board(['ETM,CHKSM,OFF']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => [document.getElementById('g-etm-chksm').checked, boardConfigs[1].etm.checksumEnabled])).toEqual([false, false]);
  expect(await sent(page, 1)).toEqual(['1:?ETM,CHKSM,ON']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.general_fake_etm_fields the ETM numbers typed in General: the message count clamps to 10-200, an emptied timeout, heartbeat, missed count or boot heartbeat falls back to the firmware default (500, 10, 5, 2), the delay keeps 0, and every board\'s config and push follow', async ({ page }) => {
  // onGeneralETMChange (app.js:1425-1457). The delay (0, and its 5000 clamp) is wizard.push_fake_etm_delay's.
  // Row general.etm_fields.
  await setup(page);
  await pullFake(page, 1, board(['ETM,TIMEOUT,600', 'ETM,HB,15', 'ETM,MISS,7', 'ETM,BOOT,4', 'ETM,COUNT,50', 'ETM,DELAY,0']));
  await pullFake(page, 2, board(['ETM,TIMEOUT,600', 'ETM,HB,15', 'ETM,MISS,7', 'ETM,BOOT,4', 'ETM,COUNT,50', 'ETM,DELAY,0'], { wcb: 2 }));
  await holdVerifyPulls(page);
  const etm = (n) => page.evaluate((n) => { const e = boardConfigs[n].etm;
    return [e.timeoutMs, e.heartbeatSec, e.missedHeartbeats, e.bootHeartbeatSec, e.messageCount, e.messageDelayMs]; }, n);
  expect(await etm(1)).toEqual([600, 15, 7, 4, 50, 0]);

  await edit(page, '#g-etm-count', '3');
  expect([await etm(1), await etm(2)]).toEqual([[600, 15, 7, 4, 10, 0], [600, 15, 7, 4, 10, 0]]);
  expect(await sent(page, 1)).toEqual(['1:?ETM,COUNT,10']);
  await edit(page, '#g-etm-count', '999');
  expect((await etm(2))[4]).toBe(200);
  expect(await sent(page, 2)).toEqual(['2:?ETM,COUNT,200']);
  await edit(page, '#g-etm-count', '50');

  for (const id of ['g-etm-timeout', 'g-etm-hb', 'g-etm-miss', 'g-etm-boot']) await edit(page, `#${id}`, '');
  expect([await etm(1), await etm(2)], 'the firmware defaults, and the delay\'s 0 kept')
    .toEqual([[500, 10, 5, 2, 50, 0], [500, 10, 5, 2, 50, 0]]);
  expect(await page.evaluate(() => { const f = generalBaseline.fields; return [f.etmTimeout, f.etmHb, f.etmMiss, f.etmBoot, f.etmCount, f.etmDelay]; }))
    .toEqual([500, 10, 5, 2, 50, 0]);
  expect(await sent(page, 1)).toEqual(['1:?ETM,TIMEOUT,500', '1:?ETM,HB,10', '1:?ETM,MISS,5', '1:?ETM,BOOT,2']);
  expect(page.wizErrors).toEqual([]);
});

const conflictRows = (page) => page.$$eval('#general-conflict-body tbody tr td:first-child', (tds) => tds.map((t) => t.textContent));
const modalOpen = (page) => page.evaluate(() => document.getElementById('general-conflict-modal').classList.contains('open'));

test('wizard.general_fake_mismatch_modal a later pull whose General differs opens the keep/use modal - but not for a factory-default board (MAC 00:00) or while the setup wizard is open; Use takes the NaviCore controller and id too and marks every other board unsaved; after a file load the modal names "loaded file"', async ({ page }) => {
  // boardPull's General check (app.js:7541-7554), isDefaultNetworkSettings (1671-1674), showGeneralMismatchModal's
  // Use (6779-6842), loadSystemFileContent's 'file' baseline (9556-9569). Use re-derived the General baseline before
  // it applied the NaviCore fields, so the board whose values were taken opened the modal again on its next pull:
  // fixed 2026-10-12 (updateGeneralBaseline after them). Row general.mismatch_modal.
  await setup(page);
  await pullFake(page, 1, board());
  // A factory-default board: MAC 00:00 and the factory password. No modal, and General keeps the first board's.
  await pullFake(page, 2, board(['MAC,2,00', 'MAC,3,00', 'EPASS,change_me_or_risk_takeover'], { wcb: 2 }));
  await page.waitForTimeout(400);                 // the modal opens 200 ms after the pull
  expect(await modalOpen(page)).toBe(false);
  expect(await page.evaluate(() => [document.getElementById('g-mac2').value, generalBaseline.sourceBoard])).toEqual(['05', 1]);
  // The setup wizard open: it owns General, so a differing board opens nothing.
  await page.evaluate(() => { _wizardOpen = true; });
  await pullFake(page, 3, board(['EPASS,dummy_other_mesh_password'], { wcb: 3, wcbq: 2 }));
  await page.waitForTimeout(400);
  expect(await modalOpen(page)).toBe(false);
  await page.evaluate(() => { _wizardOpen = false; });

  // A board on NaviCore id 15: the modal names both NaviCore rows; Use takes them.
  await setup(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([], { wcb: 2 }));
  await pullFake(page, 3, board(['CONTROLLER,ON,15'], { wcb: 3, wcbq: 2 }));
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible();
  expect(await conflictRows(page)).toEqual(['NaviCore', 'NaviCore ID']);
  expect(await page.locator('#general-conflict-use').textContent()).toBe('Use WCB 3 values');
  await page.locator('#general-conflict-use').click();
  expect(await page.evaluate(() => [systemConfig.general.controller, systemConfig.general.specialPeer, systemConfig.general.specialPeerId,
                                    document.getElementById('g-navicore-id').value,
                                    [1, 2, 3].map((n) => [boardConfigs[n].specialPeer, boardConfigs[n].specialPeerId])]))
    .toEqual(['navicore', true, 15, '15', [[true, 15], [true, 15], [true, 15]]]);
  expect(await page.evaluate(() => [1, 2, 3].map((n) => document.getElementById(`b${n}-unsaved-badge`).style.display !== 'none')),
         'every board but the one whose values were taken').toEqual([true, true, false]);
  expect(await page.evaluate(() => generalBaseline.sourceBoard)).toBe(3);
  await holdVerifyPulls(page);
  expect(await sent(page, 1)).toEqual(['1:?CONTROLLER,ON,15']);
  expect((await push(page, 3)).outcome.reason).toBe('no changes to push');
  // Pulled again, WCB3 matches the new baseline: no second modal.
  await pull(page, 3, board(['CONTROLLER,ON,15'], { wcb: 3, wcbq: 2 }));
  await page.waitForTimeout(400);
  expect(await modalOpen(page)).toBe(false);

  // After a file load the baseline is the file's: a differing board is compared with "loaded file".
  await setup(page);
  const file = ['# Droid: HIL', '[GENERAL]', ['?WCBQ,2', '?WCBCH,1', '?EPASS,dummy_not_a_real_mesh_password', '?MAC,2,05', '?MAC,3,4B'].join('^'),
                '[WCB1]', '?WCB,1^?HW,24'].join('\n');
  await page.evaluate((t) => loadSystemFileContent(t), file);
  expect(await page.evaluate(() => generalBaseline.sourceBoard)).toBe('file');
  await pullFake(page, 1, board(['MAC,3,4C']));
  await expect(page.locator('#general-conflict-modal.open')).toBeVisible();
  expect(await page.locator('#general-conflict-body').textContent()).toContain('than loaded file');
  expect(await page.locator('#general-conflict-keep').textContent()).toBe('Keep loaded file values');
  expect(await conflictRows(page)).toContain('MAC Octet 3');
  await page.locator('#general-conflict-keep').click();
  expect(await page.evaluate(() => [boardConfigs[1].macOctet3, document.getElementById('b1-unsaved-badge').style.display !== 'none']))
    .toEqual(['4B', true]);
  expect(page.wizErrors).toEqual([]);
});
