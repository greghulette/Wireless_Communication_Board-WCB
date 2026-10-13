// Needs no board: the row editors on a board card - WLED and Maestro rows, the Maestro routing table every board
// carries, mapping cards, and the sequence editor's IF check (Wizard/app.js) - medium-risk gaps the 2026-10-12 coverage
// re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json, named at the
// end of each test's comment). Fake boards in the real page (lib/fake.js): boardPull and boardGo run as on the bench,
// the handlers are reached through the inputs' own events, and only the wire is recorded. Runs standalone, in CI, and
// under the harness by id (s30_wizard.py). Verify pulls are held back where a spec pushes more than once
// (lib/rescan.js holdVerifyPulls).
const { test, expect } = require('../lib/fixtures');
const { board, pullFake, push, edit } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince, options, shown, sent } = require('../lib/rescan');

const rowIds = (page, tbody) => page.evaluate((tbody) => [...document.querySelectorAll(`#${tbody} tr`)].map((r) => r.id), tbody);
const rowNumbers = (page, tbody) => page.evaluate((tbody) => [...document.querySelectorAll(`#${tbody} tr`)].map((r) => r.cells[0].textContent), tbody);
const selectOptions = (page, sel) => page.evaluate((sel) => [...document.querySelector(sel).options].map((o) => o.value), sel);

test('wizard.wled_fake_rows a new WLED row takes the lowest id no local or remote WLED uses, its port picker offers only free ports, Remove renumbers the rows; and the S3-S5 baud cap differs by path - 57600 for a row built on S3-S5, 9600 once a port is picked (pinned for a product call)', async ({ page }) => {
  // addWLEDRow, appendWLEDRow, refreshWLEDPortDropdown, removeWLEDRow (app.js:3028-3116), onWLEDPortChange (3147-3158).
  // Which S3-S5 cap is right is not decided here: the firmware takes up to 57600 there (WCB_WLED.cpp, setWLEDConfig).
  // Row wled.rows.
  await setup(page);
  // WLED 1 local on S1 (with the reservation the board makes for it: label, broadcasts off); WLED 2 hosted on WCB3
  // (auto-learned, read-only here); an MP3 on S4.
  await pullFake(page, 1, board(['BAUD,S1,115200', 'LABEL,S1,WLED 1', 'BCAST,OUT,S1,OFF', 'BCAST,IN,S1,OFF', 'WLED,1:W1S1:115200',
                                 'WLED,2:W3S0:115200', 'MP3,S4:9600:V0']));
  await holdVerifyPulls(page);
  expect(await rowNumbers(page, 'b1-wled-tbody')).toEqual(['1', '2']);
  await page.evaluate(() => addWLEDRow(1));
  const ids = await rowIds(page, 'b1-wled-tbody');
  const added = ids[2];
  expect(await page.evaluate((r) => document.getElementById(`${r}-id`).value, added), 'not 1 (local) nor 2 (remote)').toBe('3');
  expect(await selectOptions(page, `#${added}-id`)).toEqual(['1', '2', '3', '4', '5', '6', '7', '8']);
  expect(await selectOptions(page, `#${added}-port`), 'S1 the other row\'s, S4 the MP3\'s').toEqual(['', '2', '3', '5']);
  expect(await rowNumbers(page, 'b1-wled-tbody')).toEqual(['1', '2', '3']);

  // A port picked on S3-S5: the baud list stops at 9600, and the push carries it.
  await edit(page, `#${added}-port`, '3');
  expect(await selectOptions(page, `#${added}-baud`)).toEqual(['9600']);
  expect(await sent(page, 1)).toEqual(['1:?LABEL,S3,WLED 3', '1:?BCAST,OUT,S3,OFF', '1:?BCAST,IN,S3,OFF', '1:?WLED,1:W1S1:115200', '1:?WLED,3:W1S3:9600']);
  // Back on a hardware port, every rate again.
  await edit(page, `#${added}-port`, '2');
  expect(await selectOptions(page, `#${added}-baud`)).toEqual(['9600', '19200', '38400', '57600', '115200']);

  // Remove the first row: the others are renumbered, the local table loses WLED 1 (cleared by id, never ?WLED,CLEAR).
  await page.evaluate((r) => removeWLEDRow(1, r), ids[0]);
  expect(await rowNumbers(page, 'b1-wled-tbody')).toEqual(['1', '2']);
  const r = await sent(page, 1);
  expect(r).toContain('1:?WLED,CLEAR,1');
  expect(r).not.toContain('1:?WLED,CLEAR');

  // A WLED the board already has on S4 at 57600: the row is built with S3-S5's 57600 cap.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S4,57600', 'WLED,1:W1S4:57600']));
  const row = (await rowIds(page, 'b1-wled-tbody'))[0];
  expect(await selectOptions(page, `#${row}-baud`)).toEqual(['9600', '19200', '38400', '57600']);
  expect(await page.evaluate((r) => document.getElementById(`${r}-baud`).value, row)).toBe('57600');
  // Every id taken: the id offered is the last (no 9th slot).
  await page.evaluate(() => { boardConfigs[1].wledRemotes = [2, 3, 4, 5, 6, 7, 8].map((id) => ({ id, host: 3, baud: 115200 })); addWLEDRow(1); });
  expect(await page.evaluate(() => boardConfigs[1].wleds.map((w) => w.id))).toEqual([1]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.maestro_fake_rows a new Maestro row is id 1 at 115200; its port picker hides ports HCR, WLED, a local Kyber or another Maestro row hold but keeps S1 under Maestro REMOTE\'s reservation; S3-S5 cap its bauds at 57600; Remove renumbers and a board left with no Maestro leaves remote', async ({ page }) => {
  // addMaestroRow, appendMaestroRow, refreshMaestroPortDropdown (app.js:3657-3741), onMaestroPortChange (3753-3770),
  // removeMaestroRow (3897-3907). Row maestro.rows.
  await setup(page);
  await pullFake(page, 1, board(['HCR,PORT,S4:9600', 'HCR,POLL,10', 'BAUD,S2,115200', 'WLED,1:W1S2:115200', 'MAESTRO,REMOTE']));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, boardConfigs[1].serialPorts[0].claimedBy?.type])).toEqual(['remote', 'kyber-reserved']);
  await page.evaluate(() => addMaestroRow(1));
  let [r1] = await rowIds(page, 'b1-maestro-tbody');
  expect(await page.evaluate((r) => [document.getElementById(`${r}-id`).value, document.getElementById(`${r}-baud`).value], r1)).toEqual(['1', '115200']);
  expect(await selectOptions(page, `#${r1}-port`), 'S1 kept for the remote Maestro; S2 the WLED\'s, S4 the HCR\'s').toEqual(['', '1', '3', '5']);
  await edit(page, `#${r1}-port`, '3');
  expect(await selectOptions(page, `#${r1}-baud`)).toEqual(['110', '300', '600', '1200', '2400', '9600', '14400', '19200', '38400', '57600']);
  expect(await page.evaluate((r) => document.getElementById(`${r}-baud`).value, r1), '115200 clamped').toBe('57600');
  expect(await page.evaluate(() => boardConfigs[1].maestros)).toEqual([{ id: 1, port: 3, baud: 57600 }]);
  // A second row: the first row's S3 is no longer offered to it.
  await page.evaluate(() => addMaestroRow(1));
  const [, r2] = await rowIds(page, 'b1-maestro-tbody');
  expect(await selectOptions(page, `#${r2}-port`)).toEqual(['', '1', '5']);
  await edit(page, `#${r2}-id`, '2');
  await edit(page, `#${r2}-port`, '1');
  expect(await rowNumbers(page, 'b1-maestro-tbody')).toEqual(['1', '2']);
  // Remove the first: renumbered, still remote (a Maestro is left).
  await page.evaluate((r) => removeMaestroRow(1, r), r1);
  expect(await rowNumbers(page, 'b1-maestro-tbody')).toEqual(['1']);
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, boardConfigs[1].maestros])).toEqual(['remote', [{ id: 2, port: 1, baud: 115200 }]]);
  // ...and the last: the board leaves remote.
  await page.evaluate((r) => removeMaestroRow(1, r), r2);
  expect(await page.evaluate(() => [boardConfigs[1].kyber.mode, boardConfigs[1].maestros])).toEqual(['none', []]);

  // Under a local Kyber on S2, a new row is not offered S2.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'BCAST,OUT,S2,OFF', 'BCAST,IN,S2,OFF', 'KYBER,LOCAL,S2']));
  await page.evaluate(() => addMaestroRow(1));
  [r1] = await rowIds(page, 'b1-maestro-tbody');
  expect(await selectOptions(page, `#${r1}-port`)).toEqual(['', '1', '3', '4', '5']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.maestro_fake_routing_table a Maestro edit on one board rebuilds every board\'s routing table (and a local Kyber\'s targets), marks the boards whose table changed unsaved, says how many need a re-push, and the other board\'s push then sends the whole ?MAESTRO table', async ({ page }) => {
  // rebuildMaestroRoutingTables and onMaestroChange (app.js:3779-3826); the Maestro block (parser.js:1591-1630).
  // Row maestro.routing_table.
  await setup(page);
  await pullFake(page, 1, board([], { wcbq: 3 }));
  await pullFake(page, 2, board([], { wcb: 2, wcbq: 3 }));
  await pullFake(page, 3, board(['BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'BCAST,OUT,S2,OFF', 'BCAST,IN,S2,OFF', 'KYBER,LOCAL,S2'], { wcb: 3, wcbq: 3 }));
  await holdVerifyPulls(page);
  expect(await page.evaluate(() => [1, 2, 3].map((n) => document.getElementById(`b${n}-unsaved-badge`).style.display !== 'none'))).toEqual([false, false, false]);

  const m = await toastMark(page);
  await page.evaluate(() => addMaestroRow(2));
  await edit(page, '#b2-maestro-tbody tr [id$="-port"]', '1');
  const M = { id: 1, wcb: 2, port: 1, baud: 115200 };
  expect(await page.evaluate(() => [1, 2, 3].map((n) => boardConfigs[n].maestroTable))).toEqual([[M], [M], [M]]);
  expect(await page.evaluate(() => boardConfigs[3].kyber.targets), 'the local Kyber\'s targets').toEqual([M]);
  expect(await page.evaluate(() => [1, 2, 3].map((n) => document.getElementById(`b${n}-unsaved-badge`).style.display !== 'none'))).toEqual([true, true, true]);
  expect(await toastsSince(page, m)).toContain('Maestro routing updated on 2 other boards — re-push them (or Push All)');
  expect(await sent(page, 1)).toEqual(['1:?MAESTRO,M1:W2S1:115200']);
  expect(await sent(page, 2)).toEqual(['2:?BAUD,S1,115200', '2:?LABEL,S1,Maestro 1', '2:?MAESTRO,M1:W2S1:115200']);
  expect((await sent(page, 3)).filter((s) => /MAESTRO|KYBER/.test(s))).toEqual(['3:?MAESTRO,M1:W2S1:115200', '3:?KYBER,LOCAL,S2,M1:W2S1:115200']);

  // A second edit that changes nothing in the table (the same Maestro picked again) toasts no re-push.
  const m2 = await toastMark(page);
  await edit(page, '#b2-maestro-tbody tr [id$="-port"]', '1');
  expect(await toastsSince(page, m2)).not.toContain('Maestro routing updated');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.mapping_fake_rows a mapping card: the destination WCB list covers the quantity, the boards the mesh reported and the configured value (a pulled W9 is kept); the type switch hides Raw and Bidir for PWM and rebuilds the destination ports without S0; the source list offers free, PWM and serial-map ports', async ({ page }) => {
  // appendMappingRow, refreshMappingSourceDropdown, onMappingTypeChange (app.js:3927-4023), appendMappingDestination
  // (4293-4340), syncMappingsToConfig (4038-4080). Row mapping.rows.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S1,57600', 'MAESTRO,M1:W1S1:57600', 'HCR,PORT,S4:9600', 'HCR,POLL,10', 'MAP,SERIAL,S2,W9S3']));
  await holdVerifyPulls(page);
  const card = await page.evaluate(() => document.querySelector('#b1-mappings-container .mapping-card').id);
  const dest = await page.evaluate((c) => document.querySelector(`#${c}-destinations .map-dest-row`).id, card);
  expect(await page.evaluate((d) => document.getElementById(`${d}-wcb`).value, dest)).toBe('9');
  expect(await selectOptions(page, `#${dest}-wcb`)).toEqual(['0', '2', '3', '4', '5', '6', '7', '8', '9']);
  expect(await page.evaluate((d) => document.getElementById(`${d}-wcb`).options[0].textContent, dest)).toBe('Local');
  expect(await selectOptions(page, `#${dest}-port`)).toEqual(['0', '1', '2', '3', '4', '5']);
  // Touched and synced, the config keeps WCB 9 - and nothing is pushed for it.
  await edit(page, `#${dest}-port`, '3');
  expect(await page.evaluate(() => boardConfigs[1].mappings)).toEqual([{ type: 'Serial', sourcePort: 2, rawMode: false, bidir: false, destinations: [{ wcbNumber: 9, port: 3 }] }]);
  expect((await push(page, 1)).outcome.reason).toBe('no changes to push');
  // The source list: S2 (its own serial map), S3 and S5 free; S1 a Maestro's and S4 the HCR's are not offered.
  expect(await selectOptions(page, `#${card}-src`)).toEqual(['', '2', '3', '5']);
  // A board the mesh reported, above the quantity, is offered in a new destination.
  await page.evaluate(() => addDiscoveredBoards([12]));
  await page.evaluate((c) => addMappingDestination(c, 1), card);
  const dest2 = await page.evaluate((c) => [...document.querySelectorAll(`#${c}-destinations .map-dest-row`)][1].id, card);
  expect(await selectOptions(page, `#${dest2}-wcb`)).toContain('12');
  expect(await page.evaluate((d) => document.getElementById(`${d}-port`).value, dest2), 'Serial starts on S0').toBe('0');

  // PWM: Raw and Bidir hidden, S0 gone from every destination - the one on S0 moves to S1.
  await edit(page, `#${card}-type`, 'PWM');
  expect(await page.evaluate((c) => ['raw', 'bidir'].map((w) => document.getElementById(`${c}-${w}-wrap`).style.visibility), card)).toEqual(['hidden', 'hidden']);
  expect(await selectOptions(page, `#${dest}-port`)).toEqual(['1', '2', '3', '4', '5']);
  expect(await page.evaluate(() => boardConfigs[1].mappings[0])).toEqual({ type: 'PWM', sourcePort: 2, rawMode: false, bidir: false,
    destinations: [{ wcbNumber: 9, port: 3 }, { wcbNumber: 0, port: 1 }] });
  // ...and back to Serial: S0 offered again.
  await edit(page, `#${card}-type`, 'Serial');
  expect(await page.evaluate((c) => ['raw', 'bidir'].map((w) => document.getElementById(`${c}-${w}-wrap`).style.visibility), card)).toEqual(['visible', 'visible']);
  expect(await selectOptions(page, `#${dest}-port`)).toEqual(['0', '1', '2', '3', '4', '5']);

  // A new card: a PWM mapping's ports are still offered to it (PWM claims: S3 its input, S4 its output), a serial map's
  // source too (S2); a Maestro's S1 is not.
  await setup(page);
  await pullFake(page, 1, board(['BAUD,S1,57600', 'MAESTRO,M1:W1S1:57600', 'MAP,PWM,S3,S4', 'MAP,SERIAL,S2,S5']));
  await page.evaluate(() => addMappingRow(1));
  const fresh = await page.evaluate(() => [...document.querySelectorAll('#b1-mappings-container .mapping-card')].pop().id);
  expect(await page.evaluate(() => boardConfigs[1].serialPorts.map((p) => p.claimedBy?.type ?? null)))
    .toEqual(['maestro', 'serial-map', 'pwm', 'pwm', null]);
  expect(await selectOptions(page, `#${fresh}-src`)).toEqual(['', '2', '3', '4', '5']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.seq_fake_validate_if a sequence with IF inside a ;t or ;w payload is refused on Test and on Save/Update with a toast naming IF and nothing is sent; IF ahead of the token, or "if" inside a payload, is not; a push sends such a row all the same (today)', async ({ page }) => {
  // validateSequenceValue (app.js:4666-4672), playSequence (4926-4964), updateSequence (4966-5077). Whether a push should
  // refuse the row too is left as it is. Row seq.validate_if.
  await setup(page);
  await pullFake(page, 1, board(['SEQ,SAVE,hello,;S1hi']));
  await holdVerifyPulls(page);
  const rid = await page.evaluate(() => document.querySelector('#b1-seq-tbody tr').id);
  const setText = (v) => page.evaluate(({ rid, v }) => { const e = document.querySelector(`#${rid} .seq-val-textarea`); e.value = v; e.dispatchEvent(new Event('input', { bubbles: true })); }, { rid, v });
  const run = async (fn) => {
    const m = await toastMark(page);
    await page.evaluate(({ fn, rid }) => { __fake.log.length = 0; return window[fn](1, rid); }, { fn, rid });
    return { sent: await page.evaluate(() => __fake.sent(1)), toasts: await toastsSince(page, m) };
  };
  for (const bad of [';t500,IF,x=1', ';S1a\n;w2,IF,lights=1', ';T250 , if , y=2']) {
    await setText(bad);
    for (const fn of ['playSequence', 'updateSequence']) {
      const r = await run(fn);
      expect.soft(r.sent, `${fn}: ${bad}`).toEqual([]);
      expect.soft(r.toasts, `${fn}: ${bad}`).toContain('IF cannot be embedded in a ;t or ;w payload');
    }
  }
  // IF ahead of the token, and the letters "if" in a payload, go through.
  await setText('IF,x=1\n;t500\n;S1gift,iffy');
  let r = await run('updateSequence');
  expect(r.sent).toEqual(['?SEQ,SAVE,hello,IF,x=1^;t500^;S1gift,iffy']);
  expect(r.toasts).not.toContain('IF cannot be embedded');
  r = await run('playSequence');
  expect(r.sent).toEqual([';SEQhello,L']);

  // The push does not check: an embedded IF typed into the row goes out with it.
  await setText(';t500,IF,x=1');
  expect(await sent(page, 1)).toEqual(['1:?SEQ,SAVE,hello,;t500,IF,x=1']);
  expect(page.wizErrors).toEqual([]);
});
