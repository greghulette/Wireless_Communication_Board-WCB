// Needs no board: the Kyber card (mode radio, port and baud pickers) and the NaviCore card's per-board Remote radio
// (Wizard/app.js) - medium-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). Fake boards in the real page
// (lib/fake.js): boardPull and boardGo run as on the bench, the handlers are reached through the inputs' own events,
// and only the wire is recorded. Runs standalone, in CI, and under the harness by id (s30_wizard.py). Verify pulls are
// held back where a spec pushes more than once (lib/rescan.js holdVerifyPulls).
const { test, expect } = require('../lib/fixtures');
const { board, pullFake, push, edit } = require('../lib/fake');
const { setup, holdVerifyPulls, shown, sent } = require('../lib/rescan');

const label = (page, n, p) => page.evaluate(({ n, p }) => [boardConfigs[n].serialPorts[p - 1].label,
  document.getElementById(`b${n}-s${p}-label`).value], { n, p });
const claim = (page, n, p) => page.evaluate(({ n, p }) => boardConfigs[n].serialPorts[p - 1].claimedBy?.type ?? null, { n, p });
const KYBER_WRAPS = ['port', 'baud', 'marc-port', 'targets'];
const wraps = (page, n) => Promise.all(KYBER_WRAPS.map((w) => shown(page, `b${n}-kyber-${w}-wrap`)));
const optionTexts = (page, id) => page.evaluate((id) => [...document.getElementById(id).options]
  .map((o) => [o.value, o.textContent, o.disabled, o.selected]), id);

test('wizard.kyber_fake_mode_radio the Kyber radio: Local claims the Kyber port (S2 by default) with the label Kyber Maestro, shows the port, baud, Marcduino and targets fields and marks the board unsaved, and the push sends the port rows and then ?KYBER,LOCAL,S2; None clears the claim and the automatic label; Remote pushes ?MAESTRO,REMOTE', async ({ page }) => {
  // onKyberChange (app.js:982-1048). Its Marcduino branch (9600, broadcasts on) needs a Marcduino port already picked,
  // and leaving Local clears it (kyber.marcduinoPort = null), so the radio comes back to Local with none - the
  // Marcduino picker is onKyberMarcPortChange's (wizard.push_fake_card_edits). Row kyber.mode_radio.
  await setup(page);
  await pullFake(page, 1, board());
  await holdVerifyPulls(page);
  await page.locator('#g-controller-seg [data-controller="kyber"]').click();
  await page.evaluate(() => { document.getElementById('b1-unsaved-badge').style.display = 'none'; });
  expect(await wraps(page, 1)).toEqual([false, false, false, false]);

  await edit(page, 'input[name="b1-kyber"][value="local"]', true);
  expect(await wraps(page, 1)).toEqual([true, true, true, true]);
  expect(await label(page, 1, 2)).toEqual(['Kyber Maestro', 'Kyber Maestro']);
  expect(await claim(page, 1, 2)).toBe('kyber');
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, boardConfigs[1].kyber.port, boardConfigs[1].kyber.marcduinoPort,
                                    document.getElementById('b1-kyber-baud').value, document.getElementById('b1-kyber-marc-port').value]))
    .toEqual(['local', 2, null, '115200', '0']);
  expect(await shown(page, 'b1-unsaved-badge')).toBe(true);
  // The S2 row is the Kyber's now: its baud picker and label locked.
  expect(await page.evaluate(() => [document.getElementById('b1-s2-baud').disabled, document.getElementById('b1-s2-label').disabled,
                                    document.getElementById('b1-s2-claim').textContent])).toEqual([true, true, 'Managed by Kyber (Maestro port)']);
  let r = await push(page, 1, { skipReboot: true });
  expect([r.sent, r.returned], 'the port rows, then the claim (it reboots)')
    .toEqual([['1:?BAUD,S2,115200', '1:?LABEL,S2,Kyber Maestro', '1:?KYBER,LOCAL,S2'], true]);

  // None: the claim and the automatic label go; nothing left to push.
  await edit(page, 'input[name="b1-kyber"][value="none"]', true);
  expect(await wraps(page, 1)).toEqual([false, false, false, false]);
  expect([await label(page, 1, 2), await claim(page, 1, 2)]).toEqual([['', ''], null]);
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, boardConfigs[1].kyber.port])).toEqual(['none', null]);
  expect((await push(page, 1)).outcome.reason).toBe('no changes to push');

  // Remote: no port of its own, and the push says so.
  await edit(page, 'input[name="b1-kyber"][value="remote"]', true);
  expect(await wraps(page, 1)).toEqual([false, false, false, false]);
  r = await push(page, 1, { skipReboot: true });
  expect([r.sent, r.returned]).toEqual([['1:?MAESTRO,REMOTE'], true]);

  // A custom label on the Kyber port survives leaving Local: only the automatic one is cleared.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S2,115200', 'LABEL,S2,Dome Kyber', 'BCAST,OUT,S2,OFF', 'BCAST,IN,S2,OFF', 'KYBER,LOCAL,S2']));
  await edit(page, 'input[name="b1-kyber"][value="none"]', true);
  expect(await label(page, 1, 2)).toEqual(['Dome Kyber', 'Dome Kyber']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.kyber_fake_port_dropdown the Kyber port picker offers S1 and S2 only, hides one any other feature holds (a local Maestro on S1), keeps the port the Kyber is on, shows a legacy S3-S5 Kyber port disabled, and says when both are taken - a choice that still pushes ?KYBER,LOCAL,S2 today', async ({ page }) => {
  // updateKyberPortDropdown (app.js:1111-1152), syncKyberToConfig (2365-2385); the ?KYBER,LOCAL emit's `|| 2`
  // (parser.js:1638-1643). Row kyber.port_dropdown.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S1,57600', 'MAESTRO,M1:W1S1:57600', 'BAUD,S2,115200', 'LABEL,S2,Kyber Maestro',
                                 'BCAST,OUT,S2,OFF', 'BCAST,IN,S2,OFF', 'KYBER,LOCAL,S2,M1:W1S1:57600']));
  expect(await optionTexts(page, 'b1-kyber-port')).toEqual([['2', 'Serial 2', false, true]]);

  // On S4 (set up before the firmware refused S3-S5): kept, shown, and not choosable again.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S4,57600', 'LABEL,S4,Kyber Maestro', 'BCAST,OUT,S4,OFF', 'BCAST,IN,S4,OFF', 'KYBER,LOCAL,S4']));
  expect(await optionTexts(page, 'b1-kyber-port')).toEqual([['1', 'Serial 1', false, false], ['2', 'Serial 2', false, false],
                                                             ['4', 'Serial 4 — software serial, move to S1/S2', true, true]]);
  await holdVerifyPulls(page);
  expect((await push(page, 1)).outcome.reason, 'read back as it is: no move').toBe('no changes to push');

  // S1 an HCR's and S2 an MP3's: Local finds no port, and says so.
  await setup(page);
  await pullFake(page, 1, board(['HCR,PORT,S1:9600', 'HCR,POLL,10', 'MP3,S2:9600:V0']));
  await holdVerifyPulls(page);
  await edit(page, 'input[name="b1-kyber"][value="local"]', true);
  expect(await optionTexts(page, 'b1-kyber-port')).toEqual([['', '— S1 and S2 both in use —', true, true]]);
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, boardConfigs[1].kyber.port])).toEqual(['local', null]);
  // What a push then sends: the claim on S2 regardless (the emit's `|| 2`), over the MP3 there.
  const r = await push(page, 1, { skipReboot: true });
  expect(r.sent.filter((s) => s.includes('KYBER'))).toEqual(['1:?KYBER,LOCAL,S2']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.kyber_fake_baud a Kyber baud change is written into the Kyber port\'s serial baud, so the push carries ?BAUD,S<port> and no Kyber line', async ({ page }) => {
  // onKyberBaudChange (app.js:1096-1109), syncKyberToConfig (2365-2385). Row kyber.baud.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'BCAST,OUT,S2,OFF', 'BCAST,IN,S2,OFF', 'KYBER,LOCAL,S2']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => document.getElementById('b1-kyber-baud').value)).toBe('115200');
  await edit(page, '#b1-kyber-baud', '57600');
  expect(await page.evaluate(() => [boardConfigs[1].kyber.baud, boardConfigs[1].serialPorts[1].baud,
                                    document.getElementById('b1-s2-baud').value])).toEqual([57600, 57600, '57600']);
  expect(await shown(page, 'b1-unsaved-badge')).toBe(true);
  expect(await sent(page, 1)).toEqual(['1:?BAUD,S2,57600']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.navicore_fake_remote_radio under the NaviCore controller only the NaviCore card shows; its Remote radio sets the board\'s Kyber mode to remote through the canonical radio, the status names the NaviCore id, and the push sends ?MAESTRO,REMOTE; None takes it back', async ({ page }) => {
  // updateRemoteSectionsUI, updateNavicoreStatusText, setBoardKyberMode, onNavicoreRemoteChange (app.js:2396-2453).
  // Row navicore.remote_radio.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S1,57600', 'MAESTRO,M1:W1S1:57600']));
  await holdVerifyPulls(page);
  const cards = () => Promise.all(['b1-kyber-section', 'b1-navicore-section'].map((id) => shown(page, id)));
  const status = () => page.evaluate(() => document.getElementById('b1-navicore-status').textContent);
  expect(await cards(), 'no controller: neither card').toEqual([false, false]);
  await page.locator('#g-controller-seg [data-controller="kyber"]').click();
  expect(await cards()).toEqual([true, false]);
  await page.locator('#g-controller-seg [data-controller="navicore"]').click();
  expect(await cards()).toEqual([false, true]);
  expect(await status()).toContain('Set to None');
  await edit(page, '#g-navicore-id', '17');

  await edit(page, 'input[name="b1-navicore-mode"][value="remote"]', true);
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, document.querySelector('input[name="b1-kyber"]:checked').value,
                                    boardConfigs[1].specialPeer, boardConfigs[1].specialPeerId])).toEqual(['remote', 'remote', true, 17]);
  expect(await status()).toContain('Remote — this WCB listens for Maestro protocol messages from NaviCore (ID 17)');
  const r = await push(page, 1, { skipReboot: true });
  expect([r.sent, r.returned]).toEqual([['1:?CONTROLLER,ON,17', '1:?MAESTRO,REMOTE'], true]);

  await edit(page, 'input[name="b1-navicore-mode"][value="none"]', true);
  expect(await page.evaluate(() => boardConfigs[1].kyber.mode)).toBe('none');
  expect(await status()).toContain('Set to None');
  expect(await sent(page, 1)).toEqual(['1:?CONTROLLER,ON,17']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.navicore_fake_reconcile_maestros a board that gains a Maestro under NaviCore goes remote, and one that loses its last Maestro leaves remote - under NaviCore or Kyber', async ({ page }) => {
  // reconcileRemoteWithMaestros (app.js:2459-2468), run by onMaestroChange (3807-3826) and removeMaestroRow (3897-3907).
  // Row navicore.reconcile_maestros.
  await setup(page);
  await pullFake(page, 1, board());
  await page.locator('#g-controller-seg [data-controller="navicore"]').click();
  await page.evaluate(() => addMaestroRow(1));
  expect(await page.evaluate(() => boardConfigs[1].kyber.mode), 'a row with no port is no Maestro yet').toBe('none');
  await edit(page, '#b1-maestro-tbody tr [id$="-port"]', '1');
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, document.querySelector('input[name="b1-navicore-mode"]:checked').value]))
    .toEqual(['remote', 'remote']);
  await page.evaluate(() => removeMaestroRow(1, document.querySelector('#b1-maestro-tbody tr').id));
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, document.querySelector('input[name="b1-navicore-mode"]:checked').value]))
    .toEqual(['none', 'none']);

  // Kyber: a remote board losing its last Maestro leaves remote; gaining one does not make a board remote.
  await setup(page);
  await pullFake(page, 2, board(['BAUD,S1,57600', 'MAESTRO,REMOTE', 'MAESTRO,M2:W2S1:57600'], { wcb: 2 }));
  expect(await page.evaluate(() => [systemConfig.general.controller, boardConfigs[2].kyber.mode])).toEqual(['kyber', 'remote']);
  await page.evaluate(() => removeMaestroRow(2, document.querySelector('#b2-maestro-tbody tr').id));
  expect(await page.evaluate(() => boardConfigs[2].kyber.mode)).toBe('none');
  await page.evaluate(() => addMaestroRow(2));
  await edit(page, '#b2-maestro-tbody tr [id$="-port"]', '1');
  expect(await page.evaluate(() => boardConfigs[2].kyber.mode)).toBe('none');
  expect(page.wizErrors).toEqual([]);
});
