// Needs no board: the sequence builder (Wizard/seq-builder.js) - a stored sequence laid out on a timeline, the command
// library (Wizard/cmdlib/, the one NaviCore's config tool presents) as its palette, and the result written back into
// the sequence row so SAVE/UPDATE, TEST and a push send it. Greg's request 2026-10-20. Fake boards in the real page
// (lib/fake.js); only the wire is recorded. Runs standalone, in CI, and under the harness by id.
// SB_SHOTS=<dir> also saves a screenshot at each step, for looking at the layout.
//
// Nothing here is a real credential: the mesh password is lib/fake.js's dummy.
const path = require('node:path');
const { test, expect } = require('../lib/fixtures');
const { board, pullFake, push } = require('../lib/fake');
const { setup } = require('../lib/rescan');

const SHOTS = process.env.SB_SHOTS || '';
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) }); };

const SHOW = ';M11^;A,PLAY,5^;T1250^;S3A001***holos^;T2750^;M12';
// Notes: one first, one inline, one on its own between steps, one after the last step.
const NOTES = '***intro^;M11***wave^;T500^^***half^;M12^^***end';
// Three devices on one unlabelled port, as in Greg's 2026-10-20 screenshot: once one "This board · S1" lane.
const MIXED = ';S1:DPA180^;T500^;S1F001^;T500^;S1<SE>';
const W1 = board(['LABEL,S3,Flthy HP Controller', 'MP3,S2:9600:V0',
                  `SEQ,SAVE,show,${SHOW}`, 'SEQ,SAVE,sub,;M13^;T500^;M14', 'SEQ,SAVE,flat,;M11^;M12',
                  `SEQ,SAVE,notes,${NOTES}`, `SEQ,SAVE,mixed,${MIXED}`]);

const rowOf = (page, key) => page.evaluate((key) => [...document.querySelectorAll('#b1-seq-tbody tr')]
  .find((r) => r.querySelector('.seq-key-input').value === key)?.id, key);
const rowValue = (page, rowId) => page.evaluate((rowId) => {
  const r = document.getElementById(rowId);
  return _seqRowValue(r, r.querySelector('.seq-val-textarea'), '^');
}, rowId);
const out = (page) => page.locator('#sb-out').textContent();
const labels = (page) => page.locator('.sb-ev:not(.sb-rev) .sb-ev-label').allTextContents();
const warnings = (page) => page.locator('#sb-warn li').allTextContents();
// Each of this sequence's lanes' colour class (sbc0..sbc7), top to bottom.
const laneSlots = (page) => page.locator('#sb-lanes > .sb-lane:not(.sb-ref):not(.sb-divider):not(.sb-lane-empty)')
  .evaluateAll((els) => els.map((e) => [...e.classList].find((c) => /^sbc/.test(c))));

async function openRow(page, key) {
  const rowId = await rowOf(page, key);
  await page.click(`#${rowId} button[title="Open on a timeline"]`);
  await page.evaluate(() => SeqBuilder.ready());
  await expect(page.locator('#sb-overlay')).toHaveClass(/open/);
  await expect(page.locator('#sb-lib .sb-libsec').first()).toBeVisible();
  return rowId;
}
// A point on the timeline at time t (ms), in lane row `lane` (0 = the first lane).
async function at(page, t, lane = 0) {
  const v = await page.evaluate(() => SeqBuilder.view());
  const inner = await page.locator('#sb-tl-inner').boundingBox();
  const lanes = await page.locator('.sb-lane').all();
  const box = await lanes[Math.min(lane, lanes.length - 1)].boundingBox();
  return { x: inner.x + v.gutter + t * v.pps / 1000, y: box.y + 18 };
}
async function dragStep(page, label, toT) {
  const pill = page.locator('.sb-ev', { hasText: label }).first();
  const b = await pill.boundingBox();
  const to = await at(page, toT);
  await page.mouse.move(b.x + 3, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + 20, b.y + b.height / 2, { steps: 3 });
  await page.mouse.move(to.x + 3, b.y + b.height / 2, { steps: 6 });
  await page.mouse.up();
}

test('wizard.seq_builder_open a stored sequence opens on the timeline as it is: one lane per device (a labelled port named), each step decoded through the command library, the output the same value, and the row shows a timing strip', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, W1);
  const rowId = await rowOf(page, 'show');
  const thumb = page.locator(`#${rowId}-thumb`);
  await expect(thumb).toBeVisible();
  await expect(thumb.locator('.seq-thumb-track i')).toHaveCount(4);
  await expect(thumb.locator('.seq-thumb-meta')).toHaveText('4.000 s · 4 cmds · 3 moments');
  await expect(page.locator(`#${await rowOf(page, 'flat')}-thumb .seq-thumb-meta`)).toHaveText('2 cmds at once');
  await page.locator('#b1-seq-tbody').scrollIntoViewIfNeeded();
  await shot(page, '01-table');

  const thumbColours = await thumb.locator('.seq-thumb-track i').evaluateAll((els) => els.map((e) => e.className));
  await openRow(page, 'show');
  await shot(page, '02-open');
  expect(await out(page)).toBe(SHOW);
  expect(await page.locator('.sb-lane-label .sb-ln-name').allTextContents())
    .toEqual(['Maestro 1', 'MP3 Trigger', 'FlthyHPs Holoprojectors']);
  expect(await page.locator('.sb-lane-label .sb-ln-sub').allTextContents()).toEqual(['This board · S3']);
  // A colour per lane, the same ones the row's timing strip used (Maestro, MP3, FlthyHPs, Maestro in time order).
  const laneColours = await laneSlots(page);
  expect(new Set(laneColours).size).toBe(3);
  expect(thumbColours).toEqual([laneColours[0], laneColours[1], laneColours[2], laneColours[0]]);
  expect((await labels(page)).sort()).toEqual(['Leia · All', 'Play Track · 5', 'Run subroutine · 1', 'Run subroutine · 2']);
  await expect(page.locator('#sb-stats')).toHaveText('4 commands · 3 moments · 4.000 s');
  // Nothing changed: Apply leaves the row exactly as it was.
  await page.click('#sb-apply');
  await expect(page.locator('#sb-overlay')).not.toHaveClass(/open/);
  expect(await rowValue(page, rowId)).toBe(SHOW);

  // Notes come back where they were, the one after the last step included, after an edit made and undone by hand.
  const notesRow = await openRow(page, 'notes');
  expect(await page.locator('.sb-mk').allTextContents()).toEqual(['intro', 'half', 'end']);
  await page.locator('.sb-ev', { hasText: 'Run subroutine · 2' }).click();
  await page.click('[data-nudge="10"]');
  await page.click('[data-nudge="-10"]');
  expect(await out(page)).toBe(NOTES);
  await page.click('#sb-apply');
  expect(await rowValue(page, notesRow)).toBe(NOTES);
});

test('wizard.seq_builder_edit a step moves to the millisecond in the inspector and by dragging (snapped), its fields re-encode the command, and Apply writes the row that SAVE/UPDATE and a push then send', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, W1);
  const rowId = await openRow(page, 'show');
  await page.locator('.sb-ev', { hasText: 'Leia' }).click();
  await expect(page.locator('#sbi-t')).toHaveValue('1250');
  await page.fill('#sbi-t', '1503');
  await page.press('#sbi-t', 'Enter');
  expect(await out(page)).toBe(';M11^;A,PLAY,5^;T1503^;S3A001***holos^;T2497^;M12');
  await page.click('[data-nudge="-1"]');
  await page.click('[data-nudge="-1"]');
  await page.click('[data-nudge="100"]');
  await expect(page.locator('#sbi-t')).toHaveValue('1601');
  await page.selectOption('#sbi-p0', 'F');                       // the designator: Front
  await expect(page.locator('#sbi-cmd')).toHaveValue(';S3F001');
  await shot(page, '03-inspector');

  await dragStep(page, 'Run subroutine · 2', 2203);               // snaps to the 10 ms grid
  expect(await out(page)).toBe(';M11^;A,PLAY,5^;T1601^;S3F001***holos^;T599^;M12');
  await page.keyboard.press('Control+z');
  expect(await out(page)).toContain(';T2399^;M12');
  await page.keyboard.press('Control+y');
  const value = ';M11^;A,PLAY,5^;T1601^;S3F001***holos^;T599^;M12';
  expect(await out(page)).toBe(value);

  await page.click('#sb-apply');
  expect(await rowValue(page, rowId)).toBe(value);
  const r = await push(page, 1);
  expect(r.sent).toContain(`1:?SEQ,SAVE,show,${value}`);
});

test('wizard.seq_builder_library the library lists the WCB group first as NaviCore does; a picked command is set up in the composer and added at the playhead, or dragged onto the timeline; a device command goes to the port labelled for it', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, W1);
  await page.click('button[onclick="SeqBuilder.openNew(1)"]');
  await page.evaluate(() => SeqBuilder.ready());
  await expect(page.locator('#sb-lib .sb-libsec').first()).toBeVisible();
  expect((await page.locator('#sb-lib-list > .sb-libsec > summary .sb-libsec-name').allTextContents())[0]).toBe('WCB');
  await shot(page, '04-new');

  // Composer: MP3 "Play track", track 7, added at the playhead (set on the ruler).
  await page.fill('#sb-lib-search', 'play track');
  await page.locator('.sb-libitem[data-c="mp3.play"]').click();
  await page.fill('#sbc-p0', '7');
  await expect(page.locator('#sb-comp-prev')).toHaveText(';A,PLAY,7');
  const p = await at(page, 500);
  await page.mouse.click(p.x, (await page.locator('#sb-ruler').boundingBox()).y + 15);
  await expect(page.locator('#sb-clock')).toHaveText('0.500 s');
  await page.click('#sb-comp-add');
  // Add goes back to the list as it was, the search kept; the new step is selected in the inspector.
  await expect(page.locator('#sb-lib-search')).toHaveValue('play track');
  await expect(page.locator('.sb-libitem[data-c="mp3.play"]')).toBeVisible();
  await expect(page.locator('#sbi-p0')).toHaveValue('7');
  await shot(page, '05-composer');
  // A device's colour is the same in the library and on the timeline (the MP3 board's home colour).
  const libSlot = await page.locator('.sb-libitem[data-c="mp3.play"]').evaluate((e) => [...e.classList].find((c) => /^sbc/.test(c)));
  expect((await laneSlots(page))[0]).toBe(libSlot);

  // Drag and drop: FlthyHPs "Leia" lands on the S3 port labelled Flthy HP Controller.
  await page.fill('#sb-lib-search', 'leia');
  const to = await at(page, 1000);
  await page.locator('.sb-libitem[data-c="flthy.led.leia"]').dragTo(page.locator('#sb-tl'), {
    targetPosition: { x: to.x - (await page.locator('#sb-tl').boundingBox()).x, y: 60 },
  });
  expect(await out(page)).toBe(';T500^;A,PLAY,7^;T500^;S3F001');
  await shot(page, '06-dropped');

  // A key, then Apply makes a new row.
  await page.fill('#sb-key', 'show');
  await expect(page.locator('#sb-key-err')).toHaveText('This board already has a sequence "show".');
  await expect(page.locator('#sb-apply')).toBeDisabled();
  await page.fill('#sb-key', 'intro');
  await page.click('#sb-apply');
  const rowId = await rowOf(page, 'intro');
  expect(await rowValue(page, rowId)).toBe(';T500^;A,PLAY,7^;T500^;S3F001');
});

test('wizard.seq_builder_rules the builder warns where the board would not run it as drawn: a timed sequence started mid-way, a step that runs this sequence itself; IF steps write the condition after their delay; Save & Test saves and runs it on the board', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, W1);
  const rowId = await openRow(page, 'show');
  // A step at the playhead, typed by hand.
  await page.click('#sb-custom');
  await page.fill('#sbi-cmd', ';Csub');
  await page.press('#sbi-cmd', 'Tab');
  expect((await warnings(page)).join('\n')).toMatch(/"sub" has delays of its own/);
  await page.fill('#sbi-cmd', ';Cshow');
  await page.press('#sbi-cmd', 'Tab');
  expect((await warnings(page)).join('\n')).toMatch(/runs this sequence itself/);
  await expect(page.locator('#sb-apply')).toBeDisabled();
  await page.keyboard.press('Delete');
  expect(await out(page)).toBe(SHOW);

  await page.locator('.sb-ev', { hasText: 'Run subroutine · 2' }).click();
  await page.fill('#sbi-if', 'mode=2');
  expect(await out(page)).toBe(';M11^;A,PLAY,5^;T1250^;S3A001***holos^;T2750^IF,mode=2^;M12');
  expect((await warnings(page)).join('\n')).toMatch(/checked when the sequence starts/);
  await shot(page, '07-rules');

  await page.evaluate(() => { __fake.log.length = 0; });
  await page.click('#sb-savetest');
  await expect.poll(() => page.evaluate(() => __fake.sent(1))).toEqual([
    '?SEQ,SAVE,show,;M11^;A,PLAY,5^;T1250^;S3A001***holos^;T2750^IF,mode=2^;M12',
    ';SEQshow,L',
  ]);
  expect(await rowValue(page, rowId)).toBe(';M11^;A,PLAY,5^;T1250^;S3A001***holos^;T2750^IF,mode=2^;M12');
  await expect(page.locator('#sb-play')).toHaveText('■ Stop');   // the preview runs alongside the board
});

test('wizard.seq_builder_devices three devices sent to one unlabelled port get a lane and a colour each, named by device with the port underneath', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, W1);
  await openRow(page, 'mixed');
  await shot(page, '08-devices');
  expect((await page.locator('.sb-lane-label .sb-ln-name').allTextContents()).sort())
    .toEqual(['FlthyHPs Holoprojectors', 'HCR Vocalizer', 'Roam-A-Dome']);
  expect(await page.locator('.sb-lane-label .sb-ln-sub').allTextContents()).toEqual(['This board · S1', 'This board · S1', 'This board · S1']);
  expect(new Set(await laneSlots(page)).size).toBe(3);
  expect(await out(page)).toBe(MIXED);
});

test('wizard.seq_builder_lineup other sequences line up above this one: picked from the loaded boards, drawn in their devices\' colours, started where this one runs them or where they are dragged; a dragged step sticks to their steps; remembered next time; they change nothing', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, W1);
  const rowId = await openRow(page, 'show');
  await page.click('#sb-ref-add');
  await expect(page.locator('#sb-refpick')).toBeVisible();
  const keys = await page.locator('.sb-refpick-item span').allTextContents();
  expect(keys).toEqual(expect.arrayContaining(['sub', 'flat', 'notes', 'mixed']));
  expect(keys).not.toContain('show');                                  // not the one being edited
  await page.locator('.sb-refpick-item[data-refadd="1/sub"]').click();
  await expect(page.locator('.sb-ref')).toHaveCount(1);
  await expect(page.locator('.sb-rev')).toHaveCount(2);
  await expect(page.locator('.sb-ref .sb-ln-sub')).toHaveText('this board · from 0.000 s');
  expect(await out(page)).toBe(SHOW);
  // sub's steps are Maestro 1's, so they wear the Maestro 1 lane's colour.
  const revSlot = await page.locator('.sb-rev').first().evaluate((e) => [...e.classList].find((c) => /^sbc/.test(c)));
  expect(revSlot).toBe((await laneSlots(page))[0]);
  await expect(page.locator('#sb-guides .sb-guide')).toHaveCount(2);  // selected: its times run through the lanes

  // A step that runs "sub" at 2 s: the reference starts there.
  const p = await at(page, 2000);
  await page.mouse.click(p.x, (await page.locator('#sb-ruler').boundingBox()).y + 15);
  await page.click('#sb-custom');
  await page.fill('#sbi-cmd', ';Csub');
  await page.press('#sbi-cmd', 'Tab');
  await expect(page.locator('.sb-ref .sb-ln-sub')).toHaveText('this board · from 2.000 s, run by this one');
  // The new Sequences lane wears the Stored Sequences board's colour, though the step was another lane while typed.
  const seqLib = await page.locator('.sb-libsec[data-sec="b:wcb-sequences"]').evaluate((e) => [...e.classList].find((c) => /^sbc/.test(c)));
  expect((await laneSlots(page))[0]).toBe(seqLib);

  // Given a start of its own off the grid, a step dragged near one of its steps sticks to it, not to the grid.
  await page.locator('.sb-ref .sb-lane-label').click();
  await page.fill('#sbr-t', '2503');
  await page.press('#sbr-t', 'Enter');
  await expect(page.locator('.sb-ref .sb-ln-sub')).toHaveText('this board · from 2.503 s');
  await shot(page, '09-lineup');
  await dragStep(page, 'Leia', 3001);                                   // sub's second step: 2503 + 500
  expect(await out(page)).toBe(';M11^;A,PLAY,5^;T2000^;Csub^;T1003^;S3A001***holos^;T997^;M12');

  // Remembered in this browser for this sequence.
  await page.click('#sb-apply');
  await openRow(page, 'show');
  await expect(page.locator('.sb-ref .sb-ln-sub')).toHaveText('this board · from 2.503 s');
  await page.click('.sb-ref-x');
  await expect(page.locator('.sb-ref')).toHaveCount(0);
  expect(await rowValue(page, rowId)).toBe(';M11^;A,PLAY,5^;T2000^;Csub^;T1003^;S3A001***holos^;T997^;M12');
});

test('wizard.seq_builder_droidnet a DroidNet Maestro sequence runs by name: @A<name> out this board\'s Serial 0 (USB), where DroidNet listens, in a lane of its own', async ({ page }) => {
  await setup(page);
  await pullFake(page, 1, W1);
  await page.click('button[onclick="SeqBuilder.openNew(1)"]');
  await page.evaluate(() => SeqBuilder.ready());
  await page.fill('#sb-lib-search', 'droidnet');
  await page.locator('.sb-libitem[data-c="wcbmaestro.droidnet"]').click();
  await page.fill('#sbc-p0', 'MonsterMash');
  await expect(page.locator('#sb-comp-dest')).toHaveValue('W1S0');
  await expect(page.locator('#sb-comp-prev')).toHaveText(';S0@AMonsterMash');
  await page.click('#sb-comp-add');
  expect(await out(page)).toBe(';S0@AMonsterMash');
  expect(await page.locator('.sb-lane-label .sb-ln-name').allTextContents()).toEqual(['Maestro via DroidNet']);
  expect(await labels(page)).toEqual(['Run · MonsterMash']);
  expect(await page.locator('.sb-lane-label .sb-ln-sub').allTextContents()).toEqual(['This board · S0 (USB)']);
  await page.selectOption('#sbi-dest', 'W2S0');                         // DroidNet on another board's USB
  expect(await out(page)).toBe(';W2,;S0@AMonsterMash');
  await shot(page, '10-droidnet');
});
