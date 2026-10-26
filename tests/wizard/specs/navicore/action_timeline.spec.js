// The action timeline (NaviCore docs/CONFIG_TOOL.md §7b): a button tap's or switch position's actions on a timeline,
// inline in its card above its rows (design E of the "Action tiles" canvas: _atBind, config_tool/index.html), each at
// its delay after the press, in a lane per device in the WCB Wizard's device colours (_atLaneOf, _atAssignColors),
// with the taps that fire with it drawn above (not exclusive) and the stored sequence an action runs drawn under it
// (GET_WCB_SEQVAL). The rows below are one line each (buildActionRow, _arPaintTier), numbered in their device's colour,
// and open to their fields on a click. The timeline is drawn from the rows: a dragged dot sets its row's delay and the
// row goes where it fires (_atStartDrag, _arSettle), a click opens its row, a double click adds one there (_atAddAt);
// the zoom bar (_atSetView), the warnings (_atProblems), ▶ Preview and the tap's ▶ Test with the cursor running
// (_atRunCursor, _testTierActions). TL_SHOTS=<dir> saves a screenshot at each step (TL_THEME=light for the light theme).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
// Two boards: WCB 2 says its Serial 1 has the Flthy HP controller (WDP port labels, WCB_STATUS portLabels).
const ROSTER = { online: [1, 1], known: [1, 1], clients: [0, 0], temporary: [0, 0], aliases: ['', 'Dome'],
                 portLabels: [['', '', '', '', ''], ['Flthy HP Controller', '', '', '', '']], seqHash: [0, 0] };
const SEQS = { 1: { '03': ';m11^;t4000^;w2testing', '04': ';A,PLAY,1^;t2000^;A,STOP' } };
// Button 12 (mode 1), not exclusive: a single tap of one action per kind of lane, and a double tap.
const SHOW = {
  exclusive: false,
  t1: [
    { type: 'wcb_broadcast', cmd: ';W2;S1F0025' },                       // FlthyHPs: Color Projector, Front, Blue
    { type: 'mp3', fn: 1, track: 5, delay: 400 },
    { type: 'wcb_unicast', target: '1', cmd: ';M11', delay: 800, note: 'dome spin' },
    { type: 'wcb_unicast', target: '1', cmd: ';C03', delay: 1000 },      // runs sequence 03 on WCB 1
    { type: 'play', cmd: 'beep', fn: 0, delay: 1500 },
  ],
  t1note: 'Show',
  t2: [{ type: 'wcb_unicast', target: '2', cmd: ';S1wave', delay: 200 }],
};

const SHOTS = process.env.TL_SHOTS || '';
async function shot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.locator('#modal .modal').screenshot({ path: path.join(SHOTS, name + '.png') });
}
async function theme(page) {
  if (process.env.TL_THEME === 'light') await page.evaluate(() => document.body.classList.add('theme-light'));
}
// Connected, the 3 s WCB status poll has brought the roster: aliases and WCB 2's port labels.
async function connect(page, emu) {
  await T.connectUsb(page, emu);
  await page.clock.fastForward(3100);
  await expect.poll(() => page.evaluate(() => (typeof wcbPortLabels !== 'undefined' && wcbPortLabels[1]) ? wcbPortLabels[1][0] : '')).toBe('Flthy HP Controller');
}
// Button n's editor with `mapping` in place of what the config holds for it.
async function openButton(page, n, mapping) {
  if (mapping) await page.evaluate(([k, m]) => { config.mappings[k] = JSON.parse(JSON.stringify(m)); }, [String(100 + n), mapping]);
  await page.evaluate((b) => openModal(b), n);
  await expect(page.locator('#modal')).toHaveClass(/open/);
}
// A tap's card shows, and the timeline is in it.
async function showTier(page, tier) {
  await page.locator(`#tiers-container .tier-tab[data-tier="${tier}"]`).click();
  await expect(page.locator(`#tier-card-${tier} #at-inline`)).toBeVisible();
}
// The lanes as drawn, top to bottom: [key, name, where].
const lanes = (page) => page.evaluate(() => [...document.querySelectorAll('#at-svg .at-lane-name')].map((n) => {
  const sub = n.nextElementSibling && n.nextElementSibling.classList.contains('at-lane-sub') ? n.nextElementSibling.textContent : '';
  return [n.dataset.lane, n.querySelector('title').textContent.split(' — ')[0], sub];
}));
const events = (page) => page.evaluate(() => _at.events.map((e) => [e.t, e.action.type, e.action.cmd || '']));
// The tier's rows as Save would read them, and as their lines say: [number, time, device, what, note].
const rowsOf = (page, tier) => page.evaluate((t) => collectTierActions(t), tier);
const lines = (page, tier) => page.evaluate((t) => [...document.querySelectorAll(`#rows-${t} .action-row.ar`)].map((r) =>
  ['.ar-num', '.ar-time', '.ar-dev', '.ar-cmd', '.ar-note'].map((c) => r.querySelector(c).textContent)), tier);
const openAi = (page, tier) => page.evaluate((t) => [...document.querySelectorAll(`#rows-${t} .action-row.ar.open`)].map((r) => +r.dataset.ai), tier);
const dot = (page, ai) => page.locator(`#at-svg g.at-ev[data-ev="${ai}"]`);
const xOf = (page, t) => page.evaluate((tt) => document.getElementById('at-svg').getBoundingClientRect().left + _atTimeToX(tt), t);
// Drag row ai's dot to page x `toX`; `during` runs before the button is let go.
async function dragDot(page, ai, toX, { alt = false, during = null } = {}) {
  const b = await dot(page, ai).locator('.at-ev-dot').boundingBox();
  const y = b.y + b.height / 2;
  await page.mouse.move(b.x + b.width / 2, y);
  await page.mouse.down();
  if (alt) await page.keyboard.down('Alt');
  await page.mouse.move(toX, y, { steps: 6 });
  if (during) await during();
  await page.mouse.up();
  if (alt) await page.keyboard.up('Alt');
}
// Double-click the timeline at time t, on a lane (its key) or on the "+ Action" row (null).
async function dblclickAt(page, t, laneKey) {
  const y = await page.evaluate((k) => {
    const el = k ? document.querySelector(`#at-svg .at-lane-bg[data-lane="${k}"]`) : document.querySelector('#at-svg .at-new-bg');
    return document.getElementById('at-svg').getBoundingClientRect().top + +el.getAttribute('y') + 4;
  }, laneKey);
  await page.mouse.dblclick(await xOf(page, t), y);
}
const status = (page) => page.locator('#at-status');
const tierTest = (page, tier) => page.locator(`#tier-card-${tier} button[title^="Play this whole sequence"]`);

test.describe('the action timeline', () => {
  test.use({ emuOptions: { config: BENCH, roster: ROSTER, seqs: SEQS } });

  test('nctool.action_timeline_lanes a tap\'s card shows its actions on a timeline above its rows, each at its delay after the press, in the lane of the device it reaches named with where it goes, in the Wizard\'s home colours with no two alike; each row is one line, numbered and named in its dot\'s colour; the stored sequence an action runs is read from its board and drawn under it, its steps where they land; the zoom bar steps, jumps and fits; the double tap\'s card takes the timeline over, with the single tap\'s actions drawn above as firing with it (not exclusive) in the same colours, and none once the button is exclusive', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await openButton(page, 12, SHOW);
    await expect(page.locator('#tier-card-1 #at-inline')).toBeVisible();
    await expect(page.locator('#at-modal, [id^="at-open-"], [id^="at-strip-"]')).toHaveCount(0);   // no window, no strip
    expect(await events(page)).toEqual([
      [0, 'wcb_broadcast', ';W2;S1F0025'], [400, 'mp3', ''], [800, 'wcb_unicast', ';M11'], [1000, 'wcb_unicast', ';C03'], [1500, 'play', 'beep']]);
    expect(await lanes(page)).toEqual([
      ['clips', 'Clips', 'this controller'],
      ['C', 'Sequences', 'via WCB 1'],
      ['M1', 'Maestro 1', 'via WCB 1'],
      ['A', 'MP3 Trigger', ''],
      ['D:flthy-hps:W2S1', 'FlthyHPs Holoprojectors', 'WCB 2 · Dome · Serial 1'],
    ]);
    const labels = await page.evaluate(() => [...document.querySelectorAll('#at-svg g.at-ev')].map((g) => [g.dataset.lane, g.querySelector('.at-ev-label').textContent]));
    expect(labels).toEqual([
      ['clips', 'Play "beep"'], ['C', 'Run Sequence (short) · 03'], ['M1', 'Run subroutine · 1'], ['A', 'Play track 5'],
      ['D:flthy-hps:W2S1', 'Color Projector · Front · Blue']]);
    // The rows: one line each, in list order, named as the timeline names them.
    expect(await lines(page, 1)).toEqual([
      ['1', '0 s', 'FlthyHPs Holoprojectors', 'Color Projector · Front · Blue', ''],
      ['2', '0.4 s', 'MP3 Trigger', 'Play track 5', ''],
      ['3', '0.8 s', 'Maestro 1', 'Run subroutine · 1', 'dome spin'],
      ['4', '1 s', 'Sequences', 'Run Sequence (short) · 03', ''],
      ['5', '1.5 s', 'Clips', 'Play "beep"', '']]);
    expect(await openAi(page, 1)).toEqual([]);   // loaded rows start folded
    // Sequence 03 (;m11^;t4000^;w2testing), pulled from WCB 1 and drawn from its action at 1 s: steps at 1 s and 5 s.
    const ref = page.locator('#at-svg g.at-ref');
    await expect(ref).toHaveAttribute('data-state', 'ready');
    expect(emu.requests('GET_WCB_SEQVAL').map((m) => [m.wcb, m.key])).toEqual([[1, '03']]);
    // Home colours: Maestro 1, MP3 2, Sequences 3, FlthyHPs 8, the NaviCore board's clip controls 6. The double tap's
    // serial line (no home) took the first free one when the editor painted every tap; the "Text to WCB 2" only
    // sequence 03 reaches takes the next.
    expect(await page.evaluate(() => Object.fromEntries(_at.colors))).toEqual({ 'D:flthy-hps:W2S1': 7, A: 1, M1: 0, C: 2, clips: 5, 'S:W2S1': 3, 'T:2': 4 });
    expect(await ref.locator('.at-ref-step').evaluateAll((cs) => cs.map((c) => [+c.dataset.t, c.getAttribute('fill')]))).toEqual([[1000, 'var(--at-c1)'], [5000, 'var(--at-c5)']]);
    expect(await ref.locator('.at-ref-step title').allTextContents()).toEqual([
      '+0.000 s (1.000 s after the press) · ;m11 · Maestro 1', '+4.000 s (5.000 s after the press) · ;w2testing · Text to WCB 2 · Dome']);
    expect(+(await ref.locator('.at-ref-span').getAttribute('width'))).toBeCloseTo(4000 * (await page.evaluate(() => _at.pxPerMs)), 3);
    expect(await page.locator('#at-svg .at-ref-name').evaluate((n) => n.firstChild.textContent)).toBe('↳ 03 · WCB 1');
    // Each row's number wears its dot's colour.
    expect(await page.evaluate(() => [...document.querySelectorAll('#rows-1 .action-row.ar')].map((r) =>
      [r.style.getPropertyValue('--ar-c'), document.querySelector(`#at-svg g.at-ev[data-ev="${r.dataset.ai}"] .at-ev-dot`).getAttribute('fill')]))).toEqual([
      ['var(--at-c8)', 'var(--at-c8)'], ['var(--at-c2)', 'var(--at-c2)'], ['var(--at-c1)', 'var(--at-c1)'], ['var(--at-c3)', 'var(--at-c3)'], ['var(--at-c6)', 'var(--at-c6)']]);
    await shot(page, '01-single-tap');
    // The zoom bar: the view opened fitting the actions (2 s), a preset jumps, − and + step, Fit fits again.
    await expect(page.locator('#at-view')).toHaveText('view 2 s · max 30:00');
    await page.locator('#at-inline .at-pre', { hasText: '10 s' }).click();
    await expect(page.locator('#at-view')).toHaveText('view 10 s · max 30:00');
    await expect(page.locator('#at-inline .at-pre.on')).toHaveText('10 s');
    await page.locator('#at-zoom-out').click();
    await expect(page.locator('#at-view')).toHaveText('view 30 s · max 30:00');
    await page.locator('#at-zoom-in').click(); await page.locator('#at-zoom-in').click(); await page.locator('#at-zoom-in').click();
    await expect(page.locator('#at-view')).toHaveText('view 2 s · max 30:00');
    await page.locator('#at-zoom-in').click();
    await expect(page.locator('#at-view')).toHaveText('view 1 s · max 30:00');
    await expect(page.locator('#at-zoom-in')).toBeDisabled();
    await page.locator('#at-inline .at-pre', { hasText: '30 m' }).click();
    await expect(page.locator('#at-zoom-out')).toBeDisabled();
    await expect(page.locator('#at-svg .at-limit')).toHaveCount(1);   // the board's 30 minutes, at the view's end
    // Fit takes in what the sequence it runs does too (03's last step lands at 5 s), read since the editor opened.
    await page.locator('#at-zoom-fit').click();
    await expect(page.locator('#at-view')).toHaveText('view 6 s · max 30:00');
    await expect(page.locator('#at-svg .at-limit')).toHaveCount(0);
    // The double tap fires the single tap's actions too: drawn above, read only, in the same colours.
    await showTier(page, 2);
    await expect(page.locator('#tier-card-1 #at-inline')).toHaveCount(0);
    expect(await page.locator('#at-svg .at-with-name').evaluateAll((ns) => ns.map((n) => n.firstChild.textContent))).toEqual(['Single tap · fires with it']);
    expect(await page.locator('#at-svg .at-with-step').evaluateAll((cs) => cs.map((c) => [+c.dataset.t, c.getAttribute('fill')]))).toEqual([
      [0, 'var(--at-c8)'], [400, 'var(--at-c2)'], [800, 'var(--at-c1)'], [1000, 'var(--at-c3)'], [1500, 'var(--at-c6)']]);
    expect(await lanes(page)).toEqual([['S:W2S1', 'Serial 1 · Flthy HP Controller', 'WCB 2 · Dome']]);
    expect(await lines(page, 2)).toEqual([['1', '0.2 s', 'Serial 1 · Flthy HP Controller', 'wave', '']]);
    expect(await page.evaluate(() => _at.colors.get('S:W2S1'))).toBe(3);
    await shot(page, '02-double-tap');
    // Exclusive: the double tap fires alone.
    await page.locator('#modal-exclusive + .slider').click();
    await expect(page.locator('#modal-exclusive')).toBeChecked();
    await expect(page.locator('#at-svg .at-with-name')).toHaveCount(0);
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_edit a click on a dot opens its row (one open at a time) and marks the dot; a dragged dot sets its row\'s delay, sticks to another dot with a guide (Alt: to the ms), never under 0, and the row goes where it fires — after those already at its time; a delay typed in a row and Done does the same, while a row only opened and closed stays put; the grip reorders, a right-click removes; a double click on a lane\'s empty space adds an action of its kind there; the button\'s Apply saves the rows and Cancel drops them', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    const FOUR = { exclusive: false, t1: SHOW.t1.slice(0, 4) };   // 0, 400, 800, 1000
    await openButton(page, 12, FOUR);
    const cmds = async () => (await rowsOf(page, 1)).map((a) => [a.cmd || a.type, a.delay || 0]);
    expect(await cmds()).toEqual([[';W2;S1F0025', 0], ['mp3', 400], [';M11', 800], [';C03', 1000]]);
    // A click opens the dot's row; another dot's closes it.
    await dot(page, 1).locator('.at-ev-hit').click({ position: { x: 6, y: 10 } });
    expect(await openAi(page, 1)).toEqual([1]);
    await expect(dot(page, 1)).toHaveClass(/\bsel\b/);
    await dot(page, 2).locator('.at-ev-hit').click({ position: { x: 6, y: 10 } });
    expect(await openAi(page, 1)).toEqual([2]);
    await expect(dot(page, 1)).not.toHaveClass(/\bsel\b/);
    // Dragging the MP3 dot to 4 px short of 800 sticks to it, a guide drawn meanwhile: the row's delay is 800 and it
    // goes after the Maestro action already at 800.
    let guides = -1;
    await dragDot(page, 1, (await xOf(page, 800)) - 4, { during: async () => { guides = await page.locator('#at-svg .at-guide').count(); } });
    expect(guides).toBe(1);
    await expect(page.locator('#at-svg .at-guide')).toHaveCount(0);
    await expect(page.locator('#delay-1-1')).toHaveValue('800');
    expect(await cmds()).toEqual([[';W2;S1F0025', 0], [';M11', 800], ['mp3', 800], [';C03', 1000]]);
    expect((await lines(page, 1)).map((l) => [l[0], l[1], l[3]])).toEqual([['1', '0 s', 'Color Projector · Front · Blue'],
      ['2', '0.8 s', 'Run subroutine · 1'], ['3', '0.8 s', 'Play track 5'], ['4', '1 s', 'Run Sequence (short) · 03']]);
    // With Alt it lands to the ms (and goes before the 800s); dragged left of 0 it stops at 0, after the action at 0.
    const px = await page.evaluate(() => _at.pxPerMs);
    await dragDot(page, 1, (await xOf(page, 600)) - 3, { alt: true });
    const d1 = +(await page.locator('#delay-1-1').inputValue());
    expect(Math.abs(d1 - (600 - 3 / px))).toBeLessThanOrEqual(2);
    expect((await cmds()).map((c) => c[0])).toEqual([';W2;S1F0025', 'mp3', ';M11', ';C03']);
    await dragDot(page, 1, (await xOf(page, 0)) - 60, { alt: true });
    expect(await cmds()).toEqual([[';W2;S1F0025', 0], ['mp3', 0], [';M11', 800], [';C03', 1000]]);
    // A delay typed in a row: on Done the row goes where it fires.
    await T.openRow(page, 1, 3);
    await page.locator('#delay-1-3').fill('300');
    await page.locator('#action-1-3 .ar-done').click();
    expect(await openAi(page, 1)).toEqual([]);
    expect(await cmds()).toEqual([[';W2;S1F0025', 0], ['mp3', 0], [';C03', 300], [';M11', 800]]);
    expect(await page.evaluate(() => _at.events.find((e) => e.id === 3).t)).toBe(300);
    // The grip drags a row up the list: actions at one time fire in list order.
    const grip = await page.locator('#action-1-1 .ar-grip').boundingBox();
    const first = await page.locator('#action-1-0').boundingBox();
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    await page.mouse.move(grip.x + grip.width / 2, first.y + 4, { steps: 5 });
    await page.mouse.up();
    expect(await cmds()).toEqual([['mp3', 0], [';W2;S1F0025', 0], [';C03', 300], [';M11', 800]]);
    expect((await lines(page, 1)).map((l) => l[0])).toEqual(['1', '2', '3', '4']);
    await shot(page, '03-edited');
    // A right-click on a dot removes its row.
    await dot(page, 0).locator('.at-ev-hit').click({ button: 'right', position: { x: 6, y: 10 } });
    expect((await cmds()).map((c) => c[0])).toEqual(['mp3', ';C03', ';M11']);
    // A double click on the MP3 lane's empty space adds an MP3 action there, open (no builder for the MP3 editor).
    await dblclickAt(page, 1500, 'A');
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    const added = await page.evaluate(() => +document.querySelector('#rows-1 .action-row.ar.open').dataset.ai);
    await expect(page.locator(`#type-1-${added}`)).toHaveValue('mp3');
    expect(Math.abs(+(await page.locator(`#delay-1-${added}`).inputValue()) - 1500)).toBeLessThanOrEqual(3);
    await page.locator(`#delay-1-${added}`).fill('1500');
    await page.locator(`#action-1-${added} .ar-done`).click();
    expect(await cmds()).toEqual([['mp3', 0], [';C03', 300], [';M11', 800], ['mp3', 1500]]);
    // The button's Apply saves them.
    await page.locator('#modal .modal-footer .btn-success').click();
    await expect.poll(() => emu.requests('SET_CONFIG').length).toBeGreaterThan(0);
    expect(await page.evaluate(() => config.mappings['112'].t1.map((a) => [a.cmd || a.type, a.delay || 0]))).toEqual([['mp3', 0], [';C03', 300], [';M11', 800], ['mp3', 1500]]);
    // A row opened and closed with no change stays where it is, even out of time order; Cancel drops edits.
    await openButton(page, 12, { exclusive: false, t1: [{ type: 'wcb_unicast', target: '1', cmd: ';S1late', delay: 900 }, { type: 'wcb_unicast', target: '1', cmd: ';S1early', delay: 100 }] });
    await T.openRow(page, 1, 0);
    await page.locator('#action-1-0 .ar-done').click();
    expect((await rowsOf(page, 1)).map((a) => a.cmd)).toEqual([';S1late', ';S1early']);
    await dragDot(page, 1, (await xOf(page, 1200)) + 2, { alt: true });
    expect((await rowsOf(page, 1)).map((a) => a.cmd)).toEqual([';S1late', ';S1early']);   // the dragged one goes last
    await page.locator('#modal .modal-footer .btn-ghost', { hasText: 'Cancel' }).click();
    expect(await page.evaluate(() => config.mappings['112'].t1.map((a) => [a.cmd, a.delay]))).toEqual([[';S1late', 900], [';S1early', 100]]);
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_library a double click on the timeline adds an action at that time and opens the command library for it; a command with no fields lands as a new action row would route it, in its device\'s lane, numbered where it fires; one with fields uses the composer, and a device string goes to the WCB port labelled for it; Cancel leaves a blank row, which the next double click reuses; a full tap refuses more', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await openButton(page, 12, { exclusive: false, t1: [{ type: 'wcb_unicast', target: '1', cmd: ';M11', delay: 800 }] });
    // No fields: the MP3 Trigger's configured home (config.mp3Dest: WCB 2), at 300 ms, before the Maestro action.
    await dblclickAt(page, 300, null);
    await expect(page.locator('#cmdlib-modal')).toHaveClass(/open/);
    await page.locator('#cmdlib-search').fill('toggle');
    await page.locator('.cmdlib-item[data-board="wcb-mp3"][data-cmd="mp3.stop"]').click();
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    const r = await rowsOf(page, 1);
    expect(r).toHaveLength(2);
    expect(r[0]).toMatchObject({ type: 'wcb_unicast', target: '2', cmd: ';A,STOP' });
    expect(Math.abs(r[0].delay - 300)).toBeLessThanOrEqual(3);
    expect(r[1]).toEqual({ type: 'wcb_unicast', target: '1', cmd: ';M11', delay: 800 });
    expect((await lines(page, 1))[0].slice(0, 1).concat((await lines(page, 1))[0].slice(2, 4))).toEqual(['1', 'MP3 Trigger', 'Start/Stop Toggle']);
    await expect(dot(page, 1)).toHaveAttribute('data-lane', 'A');
    // Fields: the composer; WCB 2 says its Serial 1 has the Flthy HP controller, so the string goes out there.
    await dblclickAt(page, 600, 'M1');
    await page.locator('#cmdlib-search').fill('color projector');
    await page.locator('.cmdlib-item[data-board="flthy-hps"][data-cmd="flthy.led.colorproj"]').click();
    await page.locator('#cmdlib-body select[data-pname="designator"]').selectOption('R');
    await page.locator('#cmdlib-body select[data-pname="color"]').selectOption('1');
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    const fl = (await rowsOf(page, 1)).find((a) => a.cmd === ';W2;S1R0021');
    expect(fl).toMatchObject({ type: 'wcb_broadcast', cmd: ';W2;S1R0021' });
    expect(Math.abs(fl.delay - 600)).toBeLessThanOrEqual(3);
    expect((await rowsOf(page, 1)).map((a) => a.cmd)).toEqual([';A,STOP', ';W2;S1R0021', ';M11']);
    await expect(page.locator('#at-svg g.at-ev[data-lane="D:flthy-hps:W2S1"] .at-ev-label')).toHaveText('Color Projector · Rear · Red');
    await shot(page, '04-library-added');
    // Cancel: a blank row stays (said so under the timeline); the next double click fills it rather than add one.
    await dblclickAt(page, 1200, null);
    await page.locator('#cmdlib-close').click();
    await expect(page.locator('#rows-1 .action-row.ar')).toHaveCount(4);
    await expect(status(page)).toHaveText('1 action has no command yet: not on the timeline, and Save leaves it out.');
    await dblclickAt(page, 1400, null);
    await expect(page.locator('#cmdlib-modal')).toHaveClass(/open/);
    await page.locator('#cmdlib-close').click();
    await expect(page.locator('#rows-1 .action-row.ar')).toHaveCount(4);
    const blank = await page.evaluate(() => +document.querySelector('#rows-1 .action-row.ar.open').dataset.ai);
    expect(Math.abs(+(await page.locator(`#delay-1-${blank}`).inputValue()) - 1400)).toBeLessThanOrEqual(3);
    // Fill the tap to the board's ten; then a double click refuses, and opens nothing.
    await page.evaluate(() => { for (let i = 0; i < 6; i++) addActionRow(1, { type: 'mp3', fn: 3, delay: 2000 + 100 * i }); });
    await expect(page.locator('#rows-1 .action-row.ar')).toHaveCount(10);
    await expect(page.locator('#tier-card-1 .btn-add-action')).toHaveText('Max 10 actions');
    await page.locator(`#action-1-${blank} .btn-remove-action`).click();   // the blank one goes; nine stay
    await page.evaluate(() => addActionRow(1, { type: 'mp3', fn: 3, delay: 3000 }));
    await dblclickAt(page, 1600, null);
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    await expect(status(page)).toHaveText('This tap already has 10 actions, as many as the board holds. Remove one to add another.');
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_warn the timeline says where the board would not run the tap as drawn: a delay past the board\'s 30 minutes is an error until the row\'s delay is changed; two timed sequences on one WCB overlapping is warned of (the second replaces the first); clip controls are noted as skipped by Test; a row with no command yet is said to be left out', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    const d = (cmd, delay) => ({ type: 'wcb_unicast', target: '1', cmd, delay });
    await openButton(page, 12, {
      exclusive: false,
      t1: [d(';S1a', 100), d(';S1b', 200), d(';S1c', 300), d(';S1d', 400)],
      t2: [d(';S1e', 100), d(';S1f', 200), d(';S1g', 300)],
      t3: [d(';C03', 500), d(';C04', 1500), { type: 'play', cmd: 'beep', fn: 0 }, d(';S1h', 2000000)],
    });
    await showTier(page, 3);
    await page.locator('#tier-card-3 .btn-add-action').click();   // a blank row: no command yet
    await expect(page.locator('#at-svg .at-ref')).toHaveCount(2);
    await expect.poll(() => page.locator('#at-svg g.at-ref').evaluateAll((gs) => gs.map((g) => g.dataset.state))).toEqual(['ready', 'ready']);
    await expect(page.locator('#at-warn > div')).toHaveText([
      '✖ “h” has a 2000000 ms delay: this board stores at most 1800000 ms (30:00.000) and would cut it. Move it.',
      '⚠ WCB 1 runs one timed sequence at a time: starting 04 at 1.500 s replaces 03, which runs until 4.500 s.',
      'ℹ ▶ Test skips the clip controls (record, play, stop); a press runs them.',
    ]);
    await expect(status(page)).toHaveText('1 action has no command yet: not on the timeline, and Save leaves it out.');
    expect(await page.evaluate(() => _at.events.find((e) => e.action.cmd === ';S1h').t)).toBe(1800000);
    await expect(page.locator('#rows-3 .action-row.ar').nth(4)).toHaveClass(/ar-blank/);
    expect((await lines(page, 3))[4].slice(2, 4)).toEqual(['—', 'no command yet']);
    await shot(page, '05-warnings');
    // Its delay changed, the error goes; it goes where it fires (after every row, the blank one included).
    await T.openRow(page, 3, 3);
    await page.locator('#delay-3-3').fill('2500');
    await page.locator('#action-3-3 .ar-done').click();
    await expect(page.locator('#at-warn > div').first()).toHaveText(/^⚠ WCB 1 runs one timed sequence at a time/);
    expect((await rowsOf(page, 3)).map((a) => [a.cmd || a.type, a.delay || 0])).toEqual([[';C03', 500], [';C04', 1500], ['beep', 0], [';S1h', 2500]]);
    await expect(page.locator('#rows-3 .action-row.ar')).toHaveCount(5);
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_test_switch ▶ Preview with no board plays the cursor and lights each action as it passes, sending nothing; the tap\'s ▶ Test fires its rows the way a press does — each at its delay after the start, two at one delay together — with the cursor running alongside; a switch position has the timeline too, and a delay typed into its row moves its dot', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    // No board: Preview only.
    await openButton(page, 12, { exclusive: false, t1: [{ type: 'mp3', fn: 1, track: 2, delay: 500 }] });
    // The clock paused, so the cursor moves only as the test runs it: a flash lasts 180 ms of it, and a loaded machine
    // let real time carry the cursor past it before the check (the full suite, 2026-10-26).
    await page.clock.pauseAt(await page.evaluate(() => Date.now() + 50));
    await page.locator('#at-preview-1').click();
    await expect(page.locator('#at-preview-1')).toHaveText('■ Stop');
    await page.clock.runFor(580);
    await expect(page.locator('#at-svg g.at-ev.flash')).toHaveCount(1);
    await expect(page.locator('#at-svg .at-cursor')).toHaveCount(1);
    await shot(page, '06-preview');
    await page.clock.runFor(1500);
    await expect(page.locator('#at-preview-1')).toHaveText('▶ Preview');
    await expect(page.locator('#at-svg g.at-ev.flash')).toHaveCount(0);
    await expect(page.locator('#at-svg .at-cursor')).toHaveCount(0);
    expect(emu.rx).toEqual([]);
    await page.clock.resume();
    await page.evaluate(() => closeModal());
    // Connected: the tap's ▶ Test, each at its delay from the start, the cursor running.
    await connect(page, emu);
    const d = (cmd, delay) => ({ type: 'wcb_unicast', target: '1', cmd, ...(delay ? { delay } : {}) });
    await openButton(page, 12, { exclusive: false, t1: [d(';S1a', 1000), d(';S1b', 1000), d(';S1c', 0)] });
    await tierTest(page, 1).click();
    expect(await page.evaluate(() => !!_at.playing)).toBe(true);
    await T.waitRequests(emu, 'TEST_ACTION', 1);
    expect(emu.requests('TEST_ACTION').map((m) => m.action.cmd)).toEqual([';S1c']);
    await page.clock.runFor(1050);
    await T.waitRequests(emu, 'TEST_ACTION', 3);
    expect(emu.requests('TEST_ACTION').map((m) => m.action.cmd)).toEqual([';S1c', ';S1a', ';S1b']);
    expect(emu.requests('TEST_ACTION').every((m) => m.action.delay === undefined)).toBe(true);
    await page.clock.runFor(1500);
    await page.evaluate(() => closeModal());
    // A switch position: its card has the timeline; a delay typed into its row moves the dot a moment later.
    await page.locator('.switch-zone[data-sw="SB"]:visible').first().click();
    await expect(page.locator('#tier-card-p0 #at-inline')).toBeVisible();
    await page.locator('#fields-p0-0-cmd').fill(';S3HILSW');
    await page.locator('#delay-p0-0').fill('250');
    await page.clock.runFor(200);
    expect(await events(page)).toEqual([[250, 'wcb_unicast', ';S3HILSW']]);
    expect((await lines(page, 'p0'))[0].slice(0, 2)).toEqual(['1', '0.25 s']);
    await expect(dot(page, 0)).toHaveAttribute('data-t', '250');
    await showTier(page, 'p2');
    await expect(page.locator('#tier-card-p0 #at-inline')).toHaveCount(0);
    T.expectNoPageErrors(page);
  });
});

test.describe('the action timeline on a board whose firmware predates the limits', () => {
  test.use({ emuOptions: { config: BENCH, roster: ROSTER, seqs: SEQS, legacyLimits: true } });

  test('nctool.action_timeline_legacy_board a board on firmware from before ten actions a tier sends no limits, so the tool holds to the old ones: five actions a tap (+ Add action and the timeline\'s double click stop there), a delay to 65535 ms (past it the timeline says the board would wrap it; the zoom stops there), and eight pending slots (more in one press is warned of, the rest firing at once); Save refuses a config the board would cut and sends nothing; a board on current firmware lifts them', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, []);
    await T.openTool(page);
    await theme(page);
    // No board yet: the tool's own maxima.
    expect(await page.evaluate(() => ncLimits())).toEqual({ actionsPerTier: 10, maxDelayMs: 1800000, pendingSlots: 32, legacy: false });
    await connect(page, emu);
    expect(await page.evaluate(() => ncLimits())).toEqual({ actionsPerTier: 5, maxDelayMs: 65535, pendingSlots: 8, legacy: true });
    const d = (cmd, delay) => ({ type: 'wcb_unicast', target: '1', cmd, delay });
    await openButton(page, 12, { exclusive: false, t1: [d(';S1a', 100), d(';S1b', 200), d(';S1c', 300), d(';S1d', 400), d(';S1e', 500)] });
    await expect(page.locator('#tier-card-1 .btn-add-action')).toBeDisabled();
    await expect(page.locator('#tier-card-1 .btn-add-action')).toHaveText('Max 5 actions');
    await dblclickAt(page, 700, null);
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    await expect(status(page)).toHaveText('This tap already has 5 actions, as many as the board holds. Remove one to add another.');
    await expect(page.locator('#at-view')).toHaveText('view 2 s · max 1:05.535');
    expect(await page.locator('#at-inline .at-pre:not([hidden])').allTextContents()).toEqual(['2 s', '10 s', '1 m']);
    await page.evaluate(() => closeModal());
    // A triple tap queuing 10 delayed actions with the taps that fire with it, one of them past 65535 ms.
    await openButton(page, 12, {
      exclusive: false,
      t1: [d(';S1a', 100), d(';S1b', 200), d(';S1c', 300), d(';S1d', 400)],
      t2: [d(';S1e', 100), d(';S1f', 200), d(';S1g', 300)],
      t3: [d(';S1h', 500), d(';S1i', 600), d(';S1j', 70000)],
    });
    await showTier(page, 3);
    await expect(page.locator('#at-warn > div')).toHaveText([
      '✖ “j” has a 70000 ms delay: this board stores at most 65535 ms (1:05.535) and would wrap it. Move it.',
      '⚠ This press queues 10 delayed actions with the taps that fire with it: the board holds 8 at once, shared with every other button and switch, and fires the rest at once, their delays dropped (PENDING_ACTION_SLOTS; firmware before the 32 slots).',
    ]);
    await shot(page, '07-legacy-board');
    await page.evaluate(() => closeModal());
    // Save: a tap of six actions, and the 70 s delay above, are refused before anything is sent.
    await page.evaluate(() => {
      config.mappings['112'] = { exclusive: false, t1: ['a', 'b', 'c', 'd', 'e', 'f'].map((c) => ({ type: 'wcb_unicast', target: '1', cmd: ';S1' + c })),
                                 t3: [{ type: 'wcb_unicast', target: '1', cmd: ';S1j', delay: 70000 }] };
    });
    const sent = emu.requests('SET_CONFIG').length;
    await page.evaluate(() => saveConfigToBoard());
    await expect.poll(() => dialogs.map((x) => x.message)).toContainEqual(
      'Not saved — this board would drop or cut part of it:\n\n• Mode 1 button 12 single tap: 6 actions (this board stores 5)\n• Mode 1 button 12 triple tap, action 1: a 70000 ms delay (this board stores up to 65535 ms)\n\n' +
      'This board runs firmware from before 10 actions a tier and 30-minute delays: update its firmware (Firmware tab), or trim these.');
    expect(emu.requests('SET_CONFIG')).toHaveLength(sent);
    // A board on current firmware sends its limits: they lift, and the same config saves.
    emu.config.lim = { actions: 10, maxDelayMs: 1800000, pending: 32, legacy: false };
    await page.evaluate(() => sendJSON({ type: 'GET_CONFIG' }));
    await expect.poll(() => page.evaluate(() => ncLimits().actionsPerTier)).toBe(10);
    T.expectNoPageErrors(page);
  });
});
