// Needs no board: the editors that send at once instead of waiting for Push - the mapping, sequence and variable
// editors and the WLED and HCR live controls - on a USB board and on a board behind it through the relay
// (docs/hil_plan/WCB.md WCB-WP41, and the no-board halves of WCB-WP21 rows 3, 4 and 8). Fake boards in the real page
// (lib/fake.js); only the wire is recorded. The bench halves are wizard.mapping_bidir_relay, wizard.seq_var_editors
// (specs/board_more.spec.js). Runs standalone, in CI, and under the harness by id (s30_wizard.py).
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, chain, install, pullFake, push, edit } = require('../lib/fake');

// WCB1 on USB (slot 1), WCB2 managed through it (slot 2), as "Manage via relay" leaves them.
async function w1AndW2(page, w1 = [], w2 = []) {
  await openWizard(page);
  await pullFake(page, 1, board(w1, { wcbq: 2 }));
  await page.evaluate(async (w2chain) => {
    addDiscoveredBoards([2]);
    setRemoteConnected(2, 1);
    _applyRemotePulledConfig(1, 2, w2chain);
    await new Promise((r) => setTimeout(r, 1200));   // RTERM,START 3 x from setRemoteConnected and from the pull
    __fake.log.length = 0;
  }, chain(board(w2, { wcb: 2 })));
}
const frag = (s) => s.replace(/^(\?MGMT,FRAG,\d+,)[0-9A-F]{4}(,\d+,\d+,)/, '$1SID$2');
const sent1 = (page) => page.evaluate(() => __fake.sent(1).filter((s) => !s.includes('RTERM')));

// A mapping row on WCB1 built through its own controls: type, source, one destination, optional bidir. Returns its id.
async function mappingRow(page, { type = 'Serial', src, wcb, port, bidir = false }) {
  const rowId = await page.evaluate(() => { addMappingRow(1); return [...document.querySelectorAll('#b1-mappings-container .mapping-card')].at(-1).id; });
  if (type !== 'Serial') await edit(page, `#${rowId}-type`, type);
  await edit(page, `#${rowId}-src`, String(src));
  const destId = await page.evaluate((rowId) => {
    addMappingDestination(rowId, 1);
    return [...document.querySelectorAll(`#${rowId}-destinations .map-dest-row`)].at(-1).id;
  }, rowId);
  await edit(page, `#${destId}-wcb`, String(wcb));
  await edit(page, `#${destId}-port`, String(port));
  if (bidir) await edit(page, `#${rowId}-bidir`, true);
  return { rowId, destId };
}

test('wizard.editors_fake_mappings the mapping editor sends at once: Save sends the mapping and, with bidir, the reverse to the destination board through the relay; Remove sends CLEAR; a removal made while disconnected is only local and no push clears it (left as found); a PWM destination moved off WCB2 is cleared there', async ({ page }) => {
  await w1AndW2(page);
  const { rowId } = await mappingRow(page, { src: 2, wcb: 2, port: 4, bidir: true });
  await page.evaluate((rowId) => saveMappingRow(rowId, 1), rowId);
  expect((await sent1(page)).map(frag)).toEqual(['?MAP,SERIAL,S2,W2S4', '?MGMT,FRAG,2,SID,0,1,?MAP,SERIAL,S4,W1S2']);
  const maps = await page.evaluate(() => [1, 2].map((n) => boardBaselines[n].mappings.map((m) =>
    `${m.type}:S${m.sourcePort}>${m.destinations.map((d) => `W${d.wcbNumber}S${d.port}`).join(',')}`)));
  expect(maps).toEqual([['Serial:S2>W2S4'], ['Serial:S4>W1S2']]);

  await page.evaluate(() => { __fake.log.length = 0; });
  await page.evaluate((rowId) => removeMappingRow(rowId, 1), rowId);
  expect(await sent1(page)).toContain('?MAP,SERIAL,CLEAR,S2');

  // Removed while the board is not connected: the page drops it, the board keeps it, and a push sends nothing for it
  // (a removed mapping builds as nothing; docs/hil_plan/WCB.md §3 records this as left as found).
  await w1AndW2(page);
  const b = await mappingRow(page, { src: 3, wcb: 0, port: 5 });
  await page.evaluate((rowId) => saveMappingRow(rowId, 1), b.rowId);
  expect(await sent1(page)).toEqual(['?MAP,SERIAL,S3,S5']);
  await page.evaluate(async (rowId) => {
    boardConnections[1]._connected = false;
    await removeMappingRow(rowId, 1);
    boardConnections[1]._connected = true;
    __fake.log.length = 0;
  }, b.rowId);
  expect(await page.evaluate(() => __fake.toastText())).toContain('removed locally only');
  const r = await push(page, 1, { skipReboot: true });
  expect([r.sent, r.outcome.reason]).toEqual([[], 'no changes to push']);

  // A PWM mapping whose remote output moves from WCB2 S3 to S4: the new mapping, and a clear of the old output on WCB2.
  await w1AndW2(page);
  const p = await mappingRow(page, { type: 'PWM', src: 5, wcb: 2, port: 3 });
  await page.evaluate((rowId) => saveMappingRow(rowId, 1), p.rowId);
  expect(await sent1(page)).toEqual(['?MAP,PWM,S5,W2S3']);
  await page.evaluate(() => { __fake.log.length = 0; });
  await edit(page, `#${p.destId}-port`, '4');
  await page.evaluate((rowId) => saveMappingRow(rowId, 1), p.rowId);
  expect((await sent1(page)).map(frag)).toEqual(['?MAP,PWM,S5,W2S4', '?MGMT,FRAG,2,SID,0,1,?MAP,PWM,CLEAR,OUT,S3']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.editors_fake_bidir_remove (should) removing a bidirectional serial mapping also clears its mirror on the destination board (W-21)', async ({ page }) => {
  await w1AndW2(page);
  const { rowId } = await mappingRow(page, { src: 2, wcb: 2, port: 4, bidir: true });
  await page.evaluate((rowId) => saveMappingRow(rowId, 1), rowId);
  await page.evaluate(() => { __fake.log.length = 0; });
  await page.evaluate((rowId) => removeMappingRow(rowId, 1), rowId);
  expect((await sent1(page)).map(frag)).toEqual(['?MAP,SERIAL,CLEAR,S2', '?MGMT,FRAG,2,SID,0,1,?MAP,SERIAL,CLEAR,S4']);
});

test('wizard.editors_fake_seq_var the sequence and variable editors: save, rename (clear the old key first), test (local, or mesh-wide) and remove on a USB board; a remote save in one packet, and one over 198 characters as a multi-chunk session; variables set, rename, clear, refuse a bad name, set a temporary with ;V; and ?VAR,LIST parses the firmware\'s lines, through the relay too', async ({ page }) => {
  await w1AndW2(page, ['SEQ,SAVE,hello,;S1hi', 'VAR,SET,hilv,5'], ['SEQ,SAVE,far,;S2x']);
  const row = (n, i = 0) => page.evaluate(({ n, i }) => document.querySelectorAll(`#b${n}-seq-tbody tr`)[i].id, { n, i });
  const setText = (sel, v) => page.evaluate(({ sel, v }) => { const e = document.querySelector(sel); e.value = v; e.dispatchEvent(new Event('input', { bubbles: true })); }, { sel, v });
  let r1 = await row(1);
  await setText(`#${r1} .seq-val-textarea`, ';S1bye\n;S2now');
  await page.evaluate((id) => updateSequence(1, id), r1);
  await setText(`#${r1} .seq-key-input`, 'hi2');
  await page.evaluate((id) => updateSequence(1, id), r1);
  await page.evaluate((id) => playSequence(1, id), r1);
  await edit(page, '#b1-seq-test-mesh', true);
  await page.evaluate((id) => playSequence(1, id), r1);
  await page.evaluate((id) => removeSequenceRow(1, id), r1);
  expect(await sent1(page)).toEqual(['?SEQ,SAVE,hello,;S1bye^;S2now', '?SEQ,CLEAR,hello', '?SEQ,SAVE,hi2,;S1bye^;S2now',
                                     ';SEQhi2,L', ';SEQhi2', '?SEQ,CLEAR,hi2']);
  expect(await page.evaluate(() => [boardBaselines[1].sequences, boardConfigs[1].sequences])).toEqual([[], []]);

  // WCB2 through the relay: a short value in one FRAG; a long one in a session of <= 179-character chunks.
  await page.evaluate(() => { __fake.log.length = 0; });
  const r2 = await row(2);
  await setText(`#${r2} .seq-val-textarea`, ';S2y');
  await page.evaluate((id) => updateSequence(2, id), r2);
  expect((await sent1(page)).map(frag)).toEqual(['?MGMT,FRAG,2,SID,0,1,?SEQ,SAVE,far,;S2y']);
  await page.evaluate(() => { __fake.log.length = 0; });
  const long = Array.from({ length: 12 }, (_, i) => `;S2${'L'.repeat(28)}${i}`).join('\n');
  await setText(`#${r2} .seq-val-textarea`, long);
  await page.evaluate((id) => updateSequence(2, id), r2);
  const m = (await sent1(page)).map((s) => /^\?MGMT,FRAG,2,([0-9A-F]{4}),(\d+),(\d+),([\s\S]*)$/.exec(s));
  expect(m.every(Boolean) && m.length > 1).toBe(true);
  expect(new Set(m.map((x) => x[1])).size).toBe(1);
  expect(m.map((x) => +x[2])).toEqual(m.map((_, i) => i));
  expect(m.every((x) => +x[3] === m.length && x[4].length <= 179)).toBe(true);
  expect(m.map((x) => x[4]).join('')).toBe(`?SEQ,SAVE,far,${long.split('\n').join('^')}`);

  // Variables on WCB1.
  await page.evaluate(() => { __fake.log.length = 0; });
  const v = await page.evaluate(() => document.querySelector('#b1-var-tbody tr').id);
  await setText(`#${v} .var-value-input`, '9');
  await page.evaluate((id) => updateVariable(1, id), v);
  await setText(`#${v} .var-name-input`, 'hilw');
  await page.evaluate((id) => updateVariable(1, id), v);
  await setText(`#${v} .var-name-input`, 'bad name');
  await page.evaluate((id) => updateVariable(1, id), v);
  expect(await page.evaluate(() => __fake.toastText())).toContain('Invalid variable name');
  await setText(`#${v} .var-name-input`, 'hilw');
  await page.evaluate((id) => removeVariableRow(1, id), v);
  const t = await page.evaluate(() => { addTemporaryVariableRow(1); return [...document.querySelectorAll('#b1-var-tbody tr')].at(-1).id; });
  await setText(`#${t} .var-name-input`, 'hilt');
  await setText(`#${t} .var-value-input`, '-3');
  await page.evaluate((id) => updateTempVariable(1, id), t);
  expect(await sent1(page)).toEqual(['?VAR,SET,hilv,9', '?VAR,CLEAR,hilv', '?VAR,SET,hilw,9', '?VAR,CLEAR,hilw', ';V,hilt,-3']);
  expect(await page.evaluate(() => boardBaselines[1].variables)).toEqual([]);

  // ?VAR,LIST as listVariables prints it (WCB_Variables.cpp:311-318).
  const LIST = ['---- Variables ----', '  count = 7  [persistent]', '  tmp = -3  [volatile]', '  2/100 used'];
  expect(await page.evaluate((l) => [parseVarList(l), parseVarList(['---- Variables ----', '  (none)']),
                                     parseVarList(['[VAR] count = 7  [persistent]'])], LIST))
    .toEqual([[{ name: 'count', value: 7, persist: true }, { name: 'tmp', value: -3, persist: false }], [], []]);
  // Refresh through the relay: the reply comes back as the target's [TERM:2] lines.
  await page.evaluate(() => { __fake.log.length = 0; window.__refresh = refreshVariablesFromBoard(2); });
  await expect.poll(() => sent1(page).then((s) => s.map(frag))).toEqual(['?MGMT,FRAG,2,SID,0,1,?VAR,LIST']);
  await page.evaluate((l) => { for (const x of l) boardConnections[1]._handleLine(`[TERM:2]${x}`); return window.__refresh; }, LIST);
  expect(await page.evaluate(() => [...document.querySelectorAll('#b2-var-tbody tr.var-row-live')].map((r) => r.textContent.replace(/\s+/g, ' ').trim())))
    .toEqual(['count 7 Persistent on board — not in config', expect.stringContaining('Temporary')]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.editors_fake_live_controls the WLED controls send ;L<id>,<verb> (preset, brightness clamped to 255, a bad preset refused) directly or through the relay, and the HCR status modal renders the firmware\'s [HCR:...] line, polls every 3 s only while open, and says when HCR is not configured', async ({ page }) => {
  await w1AndW2(page, ['BAUD,S2,115200', 'WLED,3:W1S2:115200', 'BAUD,S4,57600', 'HCR,PORT,S4:57600'], ['BAUD,S3,57600', 'WLED,4:W2S3:57600']);
  expect(await page.evaluate(() => [document.getElementById('b1-wled-controls').style.display,
                                    [...document.getElementById('b1-wled-target').options].map((o) => o.value)])).toEqual(['', ['3']]);
  await page.evaluate(() => {
    document.getElementById('b1-wled-ps').value = '5'; wledFirePreset(1);
    document.getElementById('b1-wled-bri').value = '255'; wledSetBri(1);
    document.getElementById('b1-wled-ps').value = '0'; wledFirePreset(1);
    document.getElementById('b2-wled-ps').value = '7'; wledFirePreset(2);
  });
  await page.waitForTimeout(200);
  expect((await sent1(page)).map(frag)).toEqual([';L3,PS,5', ';L3,BRI,255', '?MGMT,FRAG,2,SID,0,1,;L4,PS,7']);
  expect(await page.evaluate(() => __fake.toastText())).toContain('Enter a preset number');

  // HCR status: the modal's text is the firmware's line, decoded (printHCRStatus, WCB_HCR.cpp:929-960).
  const LINE = '[HCR:cfg=1,port=4,poll=10,age=2,H=1,S=0,M=0,C=0,dur=1.00,ovr=0,muse=1,wav=12,pV=-1,pA=3,pB=-1,vV=80,vA=60,vB=40,rx=5,vage=1]';
  await page.evaluate((l) => { boardConnections[1].__replies['?HCR,STATUS'] = l; __fake.log.length = 0; openHCRStatusModal(1); }, LINE);
  await expect(page.locator('#stats-modal-output')).toContainText('Volume   V:80  A:60  B:40');
  const out = await page.locator('#stats-modal-output').textContent();
  expect(out).toContain('Port            : S4');
  expect(out).toContain('Playing  Vocalizer:idle  A:file 3  B:idle');
  expect(out).toContain('Muse       : ON');
  await page.waitForTimeout(3300);
  const polls = () => page.evaluate(() => __fake.log.filter((e) => e.op === 'collect' && e.s === '?HCR,STATUS').length);
  expect(await polls()).toBe(2);
  await page.evaluate(() => closeStatsModal());
  const n = await polls();
  await page.waitForTimeout(3300);
  expect(await polls(), 'no polling once closed').toBe(n);
  await page.evaluate(() => { boardConnections[1].__replies['?HCR,STATUS'] = '[HCR:cfg=0]'; openHCRStatusModal(1); });
  await expect(page.locator('#stats-modal-output')).toContainText('HCR is not configured on this board');
  await page.evaluate(() => { closeStatsModal(); openHCRStatusModal(2); });
  await expect(page.locator('#stats-modal-output')).toContainText('relay not supported yet');
  await page.evaluate(() => closeStatsModal());
  expect(page.wizErrors).toEqual([]);
});
