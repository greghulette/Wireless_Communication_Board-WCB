// Needs no board: the sequence and variable editors and the terminal drawer (Wizard/app.js) - the low-risk gaps the
// 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/
// wizard.json, named at the end of each test's comment). Fake boards in the real page (lib/fake.js): the editors and
// the terminal run as on the bench, and only the wire is recorded. Runs standalone, in CI, and under the harness by id
// (s30_wizard.py, _FAKE_RESCAN_L). Verify pulls are held back where a spec pushes (lib/rescan.js holdVerifyPulls).
//
// Nothing here is a real credential: the mesh password is lib/fake.js's dummy.
const { test, expect } = require('../lib/fixtures');
const { board, chain, pullFake, push, edit, relayFake } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince } = require('../lib/rescan');

const sentOn = (page, slot) => page.evaluate((slot) => __fake.sent(slot), slot);
const frag = (s) => s.replace(/^(\?MGMT,FRAG,\d+,)[0-9A-F]{4}(,)/, '$1SID$2');
// The sequence row of board n whose key input holds `key`: its id.
const seqRow = (page, n, key) => page.evaluate(({ n, key }) => [...document.querySelectorAll(`#b${n}-seq-tbody tr`)]
  .find((r) => r.querySelector('.seq-key-input').value === key)?.id ?? null, { n, key });
const hint = (page, rowId) => page.evaluate((id) => {
  const el = document.getElementById(`${id}-shared`);
  return el.style.display === 'none' ? null : el.textContent;
}, rowId);
const typeInto = (page, selector, value) => page.evaluate(({ selector, value }) => {
  const el = document.querySelector(selector);
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
}, { selector, value });

test('wizard.seq_fake_shared_indicator a sequence key that other loaded boards also hold shows "also on Wx" under it, on every board that has it, refreshed as a key is typed, saved or removed and as boards load', async ({ page }) => {
  // seqKeyRemoteBoards, refreshSeqSharedIndicators, refreshAllSeqSharedIndicators (app.js:4880-4920), wired into the
  // key's oninput, populateUIFromConfig, updateSequence and removeSequenceRow. Row seq.shared_indicator.
  await setup(page);
  await pullFake(page, 1, board(['SEQ,SAVE,hello,;S1x', 'SEQ,SAVE,solo,;S1y'], { wcbq: 3 }));
  const h1 = await seqRow(page, 1, 'hello'), s1 = await seqRow(page, 1, 'solo');
  expect([await hint(page, h1), await hint(page, s1)], 'one board loaded: nothing shared').toEqual([null, null]);
  await pullFake(page, 2, board(['SEQ,SAVE,hello,;S2z'], { wcb: 2, wcbq: 3 }));
  const h2 = await seqRow(page, 2, 'hello');
  expect([await hint(page, h1), await hint(page, h2), await hint(page, s1)]).toEqual(['⇄ also on W2', '⇄ also on W1', null]);
  expect(await page.evaluate((id) => document.getElementById(`${id}-shared`).title, h1))
    .toBe('This name also exists on these boards — a top-level ;C/;SEQ fires them all at once.');
  await pullFake(page, 3, board(['SEQ,SAVE,hello,;S3q'], { wcb: 3, wcbq: 3 }));
  expect(await hint(page, h1)).toBe('⇄ also on W2, W3');

  // Typing a key: the row's hint follows at once.
  await typeInto(page, `#${s1} .seq-key-input`, 'hello');
  expect(await hint(page, s1)).toBe('⇄ also on W2, W3');
  await typeInto(page, `#${s1} .seq-key-input`, 'hello2');
  expect(await hint(page, s1)).toBe(null);
  // WCB2's row removed: WCB1's hint drops W2 (its stored sequences no longer hold the key).
  await page.evaluate((id) => removeSequenceRow(2, id), h2);
  expect(await hint(page, h1)).toBe('⇄ also on W3');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.seq_fake_buttons_counts a sequence row\'s TEST and SAVE/UPDATE are enabled only while its board is connected or managed through a relay; the key shows n/15 and the value its stored length, both following what is typed', async ({ page }) => {
  // updateSequencePlayButtons (app.js:4847-4859), updateSeqKeyCount, updateSeqValCount (4742-4758), appendSequenceRow's
  // counters. Row seq.buttons_counts.
  await setup(page);
  await pullFake(page, 1, board(['SEQ,SAVE,hello,;S1x^;S2y']));
  const rowId = await seqRow(page, 1, 'hello');
  const state = () => page.evaluate((id) => ({
    test: !document.getElementById(`${id}-play`).disabled, save: !document.getElementById(`${id}-update`).disabled,
    key: document.getElementById(`${id}-key-count`).textContent, val: document.getElementById(`${id}-val-count`).textContent,
  }), rowId);
  expect(await state()).toEqual({ test: true, save: true, key: '5/15', val: '9' });
  await page.evaluate(() => { boardConnections[1]._connected = false; updateConnectionUI(1, false); });
  expect(await state()).toMatchObject({ test: false, save: false });
  await page.evaluate(() => { boardConnections[1]._connected = true; updateConnectionUI(1, true); });
  expect(await state()).toMatchObject({ test: true, save: true });
  // A row added while disconnected comes up disabled.
  await page.evaluate(() => { boardConnections[1]._connected = false; updateConnectionUI(1, false); addSequenceRow(1); });
  expect(await page.evaluate(() => [...document.querySelectorAll('#b1-seq-tbody [title="Test"]')].map((b) => b.disabled)))
    .toEqual([true, true]);

  // The counters follow the typing.
  await typeInto(page, `#${rowId} .seq-key-input`, 'hello_world');
  await typeInto(page, `#${rowId} .seq-val-textarea`, ';S1x\n;S2y\n;S3zz');
  expect(await state()).toMatchObject({ key: '11/15', val: String(';S1x^;S2y^;S3zz'.length) });

  // A board managed through a relay: enabled though it has no USB connection of its own.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board(['SEQ,SAVE,hi,;S1x'], { wcb: 2 })) });
  expect(await page.evaluate(() => [...document.querySelectorAll('#b2-seq-tbody [title="Test"], #b2-seq-tbody [title="Save/Update"]')]
    .map((b) => b.disabled))).toEqual([false, false]);
  expect(page.wizErrors).toEqual([]);
});
