// Needs no board: the stored-sequence pager (Wizard/app.js applySeqPager and its controls in index.html) - one page of
// a board's sequences, 5/10/25/50/All remembered in the browser, a filter on name or contents, page buttons. Greg's
// request 2026-10-14: boards with a hundred sequences made the section endless. Fake boards in the real page
// (lib/fake.js). Runs standalone, in CI, and under the harness by id.
//
// Nothing here is a real credential: the mesh password is lib/fake.js's dummy.
const { test, expect } = require('../lib/fixtures');
const { board, install, pullFake } = require('../lib/fake');
const { setup } = require('../lib/rescan');

// 23 sequences s01..s23; s07 and s19 mention BOOM (one in its name, one in its commands).
const SEQS = Array.from({ length: 23 }, (_, i) => {
  const k = String(i + 1).padStart(2, '0');
  if (i + 1 === 7) return `SEQ,SAVE,boom${k},;S1x${k}`;
  if (i + 1 === 19) return `SEQ,SAVE,s${k},;S2 big Boom here`;
  return `SEQ,SAVE,s${k},;S1x${k}`;
});

// The keys of board n's sequence rows on screen, in order.
const shown = (page, n = 1) => page.evaluate((n) => [...document.querySelectorAll(`#b${n}-seq-tbody tr`)]
  .filter((r) => r.style.display !== 'none').map((r) => r.querySelector('.seq-key-input').value), n);
// The pager bar: page labels in order (the current one marked *), and its info text.
const nav = (page, n = 1) => page.evaluate((n) => {
  const el = document.getElementById(`b${n}-seq-pager`);
  return {
    pages: [...el.querySelectorAll('button, .seq-pager-gap')].map((b) =>
      b.classList.contains('seq-pager-gap') ? '…' : (b.textContent + (b.getAttribute('aria-current') ? '*' : '') + (b.disabled ? '-' : ''))),
    info: el.querySelector('.seq-pager-info')?.textContent ?? null,
  };
}, n);
const go = (page, label, n = 1) => page.locator(`#b${n}-seq-pager button`, { hasText: new RegExp(`^${label}$`) }).first().click();
const filter = (page, text, n = 1) => page.fill(`#b${n}-seq-filter`, text);

test('wizard.seq_pager_pages a board with 23 sequences shows 5 at a time: page buttons with arrows, a size drop-down (5/10/25/50/All) that keeps the first sequence on screen, and the size remembered in the browser', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, board(SEQS));
  expect(await shown(page)).toEqual(['s01', 's02', 's03', 's04', 's05']);
  expect(await nav(page)).toEqual({ pages: ['‹-', '1*', '2', '3', '4', '5', '›'], info: '1–5 of 23 sequences' });
  await go(page, '›');
  expect(await shown(page)).toEqual(['s06', 'boom07', 's08', 's09', 's10']);
  await go(page, '5');
  expect(await shown(page)).toEqual(['s21', 's22', 's23']);
  expect(await nav(page)).toEqual({ pages: ['‹', '1', '2', '3', '4', '5*', '›-'], info: '21–23 of 23 sequences' });
  await page.selectOption('#b1-seq-size', '10');     // page 5 at 5 began at s21: at 10 that is page 3
  expect(await shown(page)).toEqual(['s21', 's22', 's23']);
  expect((await nav(page)).info).toBe('21–23 of 23 sequences');
  await page.selectOption('#b1-seq-size', '0');      // All
  expect((await shown(page)).length).toBe(23);
  expect(await nav(page)).toEqual({ pages: [], info: '1–23 of 23 sequences' });
  expect(await page.evaluate(() => localStorage.getItem('wcbWizard.seqPageSize'))).toBe('0');
  // A fresh page keeps it: reload as openWizard does, without its localStorage.clear().
  await page.reload();
  await page.waitForFunction(() => typeof boardManualConnect === 'function');
  await page.locator('#splash-overlay button[onclick="splashGoConfig()"]').click();
  await install(page);
  await pullFake(page, 1, board(SEQS));
  expect(await page.inputValue('#b1-seq-size')).toBe('0');
  expect((await shown(page)).length).toBe(23);
});

test('wizard.seq_pager_many_pages with more than 7 pages the bar shows the first, the last and the current page with its neighbours, gaps as an ellipsis', async ({ page }) => {
  await setup(page);
  const many = Array.from({ length: 60 }, (_, i) => `SEQ,SAVE,k${String(i + 1).padStart(2, '0')},;S1y`);
  await pullFake(page, 1, board(many));
  expect((await nav(page)).pages).toEqual(['‹-', '1*', '2', '3', '4', '…', '12', '›']);
  await go(page, '4');
  expect((await nav(page)).pages).toEqual(['‹', '1', '…', '3', '4*', '5', '…', '12', '›']);
  await go(page, '12');
  expect((await nav(page)).pages).toEqual(['‹', '1', '…', '9', '10', '11', '12*', '›-']);
  expect(await shown(page)).toEqual(['k56', 'k57', 'k58', 'k59', 'k60']);
});

test('wizard.seq_pager_filter the filter matches a name or the commands, not case-sensitive, pages what it matches, says when nothing does, and a push still carries every sequence', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, board(SEQS));
  await filter(page, 'BOOM');
  expect(await shown(page)).toEqual(['boom07', 's19']);
  expect(await nav(page)).toEqual({ pages: [], info: '1–2 of 2 matching (23 sequences)' });
  await filter(page, 'nothing-like-this');
  expect(await shown(page)).toEqual([]);
  expect((await nav(page)).info).toBe('no match among 23 sequences');
  // Hidden rows are still the board's: the push reads every one (getSequencesFromUI).
  expect(await page.evaluate(() => getSequencesFromUI(1).map((s) => s.key).length)).toBe(23);
  await filter(page, '');
  expect(await shown(page)).toEqual(['s01', 's02', 's03', 's04', 's05']);
});

test('wizard.seq_pager_add_remove "+ Add" clears the filter and turns to the last page with the new row focused; removing the only row of the last page goes back a page', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, board(SEQS.slice(0, 11)));      // 11: pages of 5, 5, 1
  await filter(page, 's0');
  await page.click('button[onclick="addSequenceRow(1)"]');
  expect(await page.inputValue('#b1-seq-filter')).toBe('');
  expect(await shown(page)).toEqual(['s11', '']);         // the last page: s11 and the new, empty row
  expect(await page.evaluate(() => document.activeElement?.classList.contains('seq-key-input')
    && document.activeElement.closest('tr').style.display !== 'none')).toBe(true);
  expect((await nav(page)).info).toBe('11–12 of 12 sequences');
  page.on('dialog', (d) => d.accept());
  // Remove both rows of the last page: it empties and the pager steps back to page 2.
  for (let k = 0; k < 2; k++) {
    const id = await page.evaluate(() => [...document.querySelectorAll('#b1-seq-tbody tr')].filter((r) => r.style.display !== 'none').pop().id);
    await page.evaluate((id) => removeSequenceRow(1, id), id);
  }
  expect(await shown(page)).toEqual(['s06', 'boom07', 's08', 's09', 's10']);
  expect(await nav(page)).toEqual({ pages: ['‹', '1', '2*', '›-'], info: '6–10 of 10 sequences' });
});

// The store's size beside the count (Greg 2026-10-15: "something like 1–6 of 6 sequences (83 of 32768 bytes)"). The WIFI
// firmware's store holds 32,768 bytes, one 'key,value' line each (Code/WCB/WCB_SeqStore.h SEQ_FILE_MAX); the page works
// it out from what a push would send, so it follows an edit before it is pushed. Only for firmware with that store.
const NEW_FW = '6.2.1_151144ROCT2026';     // a WIFI build: the file store (since fecd481, 29 Sep 2026)
const OLD_FW = '6.2.1_201144RAUG2026';     // main's build: sequences in the settings store, no byte cap to show
const seqBytes = (lines) => lines.reduce((t, l) => {
  const [, , key, ...rest] = l.split(',');
  return t + Buffer.byteLength(key) + 1 + Buffer.byteLength(rest.join(',')) + 1;
}, 0);
const bytes = (page, n = 1) => page.evaluate((n) => {
  const el = document.getElementById(`b${n}-seq-bytes`);
  return el ? { text: el.textContent, cls: el.className.replace('seq-pager-bytes', '').trim(), title: el.title } : null;
}, n);
const setFw = (page, ver, n = 1) => page.evaluate(({ n, ver }) => {
  boardConfigs[n].fwVersion = ver;
  updateBoardSwVersionDisplay(n);
}, { n, ver });
const fmt = (v) => v.toLocaleString('en-US');

test('wizard.seq_pager_bytes the pager shows the bytes the sequences take of the 32,768 the store holds, follows an edit before the push, is amber from 90 % and red past the cap, counts the whole store under a filter, and is not shown for firmware without the file store', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, board(SEQS));
  await setFw(page, NEW_FW);
  const want = seqBytes(SEQS);
  expect(await bytes(page)).toEqual({ text: `· ${fmt(want)} of 32,768 bytes`, cls: '',
    title: 'The board\'s sequence store: each sequence takes its name, its commands and 2 bytes' });
  expect((await nav(page)).info, 'the count is unchanged').toBe('1–5 of 23 sequences');
  // An edit, before any push: s01's value grows by 3 bytes.
  const ta = page.locator('#b1-seq-tbody tr').first().locator('.seq-val-textarea');
  await ta.fill((await ta.inputValue()) + 'ZZZ');
  expect((await bytes(page)).text).toBe(`· ${fmt(want + 3)} of 32,768 bytes`);
  // A filter pages what it matches; the bytes stay the whole store's.
  await filter(page, 'BOOM');
  expect((await bytes(page)).text).toBe(`· ${fmt(want + 3)} of 32,768 bytes`);
  await filter(page, '');
  // Firmware before the file store: the count alone.
  await setFw(page, OLD_FW);
  expect((await bytes(page)).text).toBe('');
  expect((await nav(page)).info).toBe('1–5 of 23 sequences');

  // Near the cap: 8 sequences of 3,690 commands each, 29,552 bytes (90 % is 29,491).
  const big = Array.from({ length: 8 }, (_, i) => `SEQ,SAVE,b${i},${'x'.repeat(3690)}`);
  await pullFake(page, 1, board(big));
  await setFw(page, NEW_FW);
  const nearB = seqBytes(big);
  expect(nearB).toBe(29552);
  expect(await bytes(page)).toEqual({ text: `· 29,552 of 32,768 bytes`, cls: 'seq-bytes-near',
    title: `${fmt(32768 - nearB)} bytes left in the board's sequence store` });
  // Past it: one more of 3,999 (the most one sequence may hold) - 33,555 bytes, red, and the title says by how much.
  await page.click('button[onclick="addSequenceRow(1)"]');
  const row = page.locator('#b1-seq-tbody tr').last();
  await row.locator('.seq-key-input').fill('zz');
  await row.locator('.seq-val-textarea').fill('y'.repeat(3999));
  const b = await bytes(page);
  expect([b.text, b.cls]).toEqual([`· 33,555 of 32,768 bytes`, 'seq-bytes-over']);
  expect(b.title).toContain('a push is refused for the ones that do not fit ("no room")');
  expect(b.title).toContain('Shorten or remove 787 bytes');
});
