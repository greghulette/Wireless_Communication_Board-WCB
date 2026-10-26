// The action timeline (NaviCore docs/CONFIG_TOOL.md §4b): ⏱ Timeline on a button tap or a switch position opens its
// actions on a timeline, each at its delay after the press, in a lane per device in the WCB Wizard's device colours
// (openActionTimeline, _atLaneOf, _atAssignColors, config_tool/index.html), with the taps that fire with it drawn
// above (not exclusive) and the stored sequence an action runs drawn under it (GET_WCB_SEQVAL); editing (_atStartDrag,
// _atSnap, _atAlign, _atSpread, _atNudge, undo/redo), the docked command library (_atLibPick, _cmdlibResolveAction),
// the warnings (_atProblems), Apply writing the rows back, the strip on each tier (_atStripRender), ▶ Test firing the
// timeline the way a press does (_testTierActions) and Preview with no board. TL_SHOTS=<dir> saves a screenshot at
// each step (TL_THEME=light for the light theme).
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
    { type: 'wcb_unicast', target: '1', cmd: ';M11', delay: 800 },
    { type: 'wcb_unicast', target: '1', cmd: ';C03', delay: 1000 },      // runs sequence 03 on WCB 1
    { type: 'play', cmd: 'beep', fn: 0, delay: 1500 },
  ],
  t1note: 'Show',
  t2: [{ type: 'wcb_unicast', target: '2', cmd: ';S1wave', delay: 200 }],
};

const SHOTS = process.env.TL_SHOTS || '';
async function shot(page, name, sel = '#at-modal .modal') {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.locator(sel).screenshot({ path: path.join(SHOTS, name + '.png') });
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
async function openTimeline(page, tier) {
  await page.locator(`#tiers-container .tier-tab[data-tier="${tier}"]`).click();
  await page.locator(`#at-open-${tier}`).click();
  await expect(page.locator('#at-modal')).toHaveClass(/open/);
}
// The lanes as drawn, top to bottom: [key, name, where].
const lanes = (page) => page.evaluate(() => [...document.querySelectorAll('#at-svg .at-lane-name')].map((n) => {
  const sub = n.nextElementSibling && n.nextElementSibling.classList.contains('at-lane-sub') ? n.nextElementSibling.textContent : '';
  return [n.dataset.lane, n.querySelector('title').textContent.split(' — ')[0], sub];
}));
const events = (page) => page.evaluate(() => _at.events.map((e) => [e.t, e.action.type, e.action.cmd || '']));
const times = (page) => page.evaluate(() => _at.events.map((e) => e.t));
const selected = (page) => page.evaluate(() => _at.events.filter((e) => _at.sel.has(e.id)).map((e) => e.t).sort((a, b) => a - b));
// A step's group, found by its time (no two steps of these tiers share one unless a test aligns them).
const stepAt = (page, t) => page.evaluate((tt) => _at.events.find((e) => e.t === tt).id, t)
  .then((id) => page.locator(`#at-svg g.at-ev[data-ev="${id}"]`));
async function shiftClick(page, t) { await (await stepAt(page, t)).locator('.at-ev-hit').click({ modifiers: ['Shift'], position: { x: 6, y: 10 } }); }
const xOf = (page, t) => page.evaluate((tt) => document.getElementById('at-svg').getBoundingClientRect().left + _atTimeToX(tt), t);
// Drag the step at time t by its dot to page x `toX`; `during` runs before the button is let go.
async function dragStep(page, t, toX, { alt = false, during = null } = {}) {
  const b = await (await stepAt(page, t)).locator('.at-ev-dot').boundingBox();
  const y = b.y + b.height / 2;
  await page.mouse.move(b.x + b.width / 2, y);
  await page.mouse.down();
  if (alt) await page.keyboard.down('Alt');
  await page.mouse.move(toX, y, { steps: 6 });
  if (during) await during();
  await page.mouse.up();
  if (alt) await page.keyboard.up('Alt');
}
const status = (page) => page.locator('#at-status');
const libCmd = (page, id) => page.locator(`#at-lib-list .at-lib-cmd[data-cmd="${id}"]`);
// The tier's rows as Save would read them.
const rowsOf = (page, tier) => page.evaluate((t) => collectTierActions(t), tier);

test.describe('the action timeline', () => {
  test.use({ emuOptions: { config: BENCH, roster: ROSTER, seqs: SEQS } });

  test('nctool.action_timeline_lanes ⏱ Timeline on a tap opens its actions each at its delay after the press, in the lane of the device it reaches named with where it goes, in the Wizard\'s home colours with no two alike; the strip on the tap shows the same dots; the stored sequence an action runs is read from its board and drawn under it, its steps where they land; a double tap of a button that is not exclusive draws the single tap\'s actions above as firing with it, in the same colours, and an exclusive one does not', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await openButton(page, 12, SHOW);
    // The strip on the single tap: a dot per action at its delay, in its device's colour.
    await page.clock.fastForward(200);
    const strip = page.locator('#at-strip-1');
    await expect(strip.locator('.at-strip-dot')).toHaveCount(5);
    expect(await strip.locator('.at-strip-dot').evaluateAll((cs) => cs.map((c) => [+c.dataset.t, c.getAttribute('fill')]))).toEqual([
      [0, 'var(--at-c8)'], [400, 'var(--at-c2)'], [800, 'var(--at-c1)'], [1000, 'var(--at-c3)'], [1500, 'var(--at-c6)']]);
    await shot(page, '00-strip', '#modal .modal');
    await openTimeline(page, 1);
    await expect(page.locator('#at-title')).toHaveText('⏱ Button 12: ' + await page.evaluate(() => getBtnLabel(12)) + ' · Single tap — “Show”');
    expect(await events(page)).toEqual([
      [0, 'wcb_broadcast', ';W2;S1F0025'], [400, 'mp3', ''], [800, 'wcb_unicast', ';M11'], [1000, 'wcb_unicast', ';C03'], [1500, 'play', 'beep']]);
    expect(await lanes(page)).toEqual([
      ['clips', 'Clips', 'this controller'],
      ['C', 'Sequences', 'via WCB 1'],
      ['M1', 'Maestro 1', 'via WCB 1'],
      ['A', 'MP3 Trigger', ''],
      ['D:flthy-hps:W2S1', 'FlthyHPs Holoprojectors', 'WCB 2 · Dome · Serial 1'],
    ]);
    // Home colours: Maestro 1, MP3 2, Sequences 3, FlthyHPs 8, the NaviCore board's clip controls 6; the "Text to
    // WCB 2" only sequence 03 reaches takes the first free one.
    expect(await page.evaluate(() => Object.fromEntries(_at.colors))).toEqual({ 'D:flthy-hps:W2S1': 7, A: 1, M1: 0, C: 2, clips: 5, 'T:2': 3 });
    const labels = await page.evaluate(() => [...document.querySelectorAll('#at-svg g.at-ev')].map((g) => [g.dataset.lane, g.querySelector('.at-ev-label').textContent]));
    expect(labels).toEqual([
      ['clips', 'Play "beep"'], ['C', 'Run Sequence (short) · 03'], ['M1', 'Run subroutine · 1'], ['A', 'Play track 5'],
      ['D:flthy-hps:W2S1', 'Color Projector · Front · Blue']]);
    await expect(page.locator('#at-count')).toHaveText('5 of 10 actions');   // this board's firmware stores 10 (its limits)
    // Sequence 03 (;m11^;t4000^;w2testing), pulled from WCB 1 and drawn from its action at 1 s: steps at 1 s and 5 s.
    const ref = page.locator('#at-svg g.at-ref');
    await expect(ref).toHaveAttribute('data-state', 'ready');
    expect(emu.requests('GET_WCB_SEQVAL').map((m) => [m.wcb, m.key])).toEqual([[1, '03']]);
    expect(await ref.locator('.at-ref-step').evaluateAll((cs) => cs.map((c) => [+c.dataset.t, c.getAttribute('fill')]))).toEqual([[1000, 'var(--at-c1)'], [5000, 'var(--at-c4)']]);
    expect(await ref.locator('.at-ref-step title').allTextContents()).toEqual([
      '+0.000 s (1.000 s after the press) · ;m11 · Maestro 1', '+4.000 s (5.000 s after the press) · ;w2testing · Text to WCB 2 · Dome']);
    expect(+(await ref.locator('.at-ref-span').getAttribute('width'))).toBeCloseTo(4000 * (await page.evaluate(() => _at.pxPerMs)), 3);
    expect(await page.locator('#at-svg .at-ref-name').evaluate((n) => n.firstChild.textContent)).toBe('↳ 03 · WCB 1');
    await page.locator('#at-zoom-fit').click();
    await shot(page, '01-single-tap');
    await page.locator('#at-close').click();
    await expect(page.locator('#at-modal')).not.toHaveClass(/open/);
    // The double tap fires the single tap's actions too: drawn above, read only, in the same colours.
    await openTimeline(page, 2);
    expect(await page.locator('#at-svg .at-with-name').evaluateAll((ns) => ns.map((n) => n.firstChild.textContent))).toEqual(['Single tap · fires with it']);
    expect(await page.locator('#at-svg .at-with-step').evaluateAll((cs) => cs.map((c) => [+c.dataset.t, c.getAttribute('fill')]))).toEqual([
      [0, 'var(--at-c8)'], [400, 'var(--at-c2)'], [800, 'var(--at-c1)'], [1000, 'var(--at-c3)'], [1500, 'var(--at-c6)']]);
    expect(await lanes(page)).toEqual([['S:W2S1', 'Serial 1 · Flthy HP Controller', 'WCB 2 · Dome']]);
    expect(await page.evaluate(() => _at.colors.get('S:W2S1'))).toBe(3);
    await shot(page, '02-double-tap');
    await page.locator('#at-close').click();
    // Exclusive: the double tap fires alone.
    await page.locator('#modal-exclusive + .slider').click();
    await expect(page.locator('#modal-exclusive')).toBeChecked();
    await openTimeline(page, 2);
    await expect(page.locator('#at-svg .at-with-name')).toHaveCount(0);
    await page.locator('#at-close').click();
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_edit Shift-click selects several steps; Align puts them at the first\'s, the last\'s or the cursor\'s time, at one time in the order they were laid out, and Space evenly evens the middle; the arrow keys nudge 10 / 100 / 1 ms; Delete removes the selection; each is one undo step and Redo puts it back; a dragged step sticks to the cursor or another step with a guide, Alt places it to the ms, never under 0; a click edits the step, a click on a lane\'s empty space starts one of its kind; Apply writes the rows in time order with their delays, which the button\'s Apply saves, and Cancel with changes asks first', async ({ page, emu }) => {
    const answers = [];
    const dialogs = T.answerDialogs(page, answers);
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    const FOUR = { exclusive: false, t1: SHOW.t1.slice(0, 4) };   // 0, 400, 800, 1000
    await openButton(page, 12, FOUR);
    await openTimeline(page, 1);
    const all0 = await times(page);
    expect(all0).toEqual([0, 400, 800, 1000]);
    await shiftClick(page, 400);
    await expect(page.locator('#at-multi')).toBeHidden();
    await shiftClick(page, 800);
    await expect(page.locator('#at-multi')).toBeVisible();
    await expect(page.locator('#at-spread')).toBeDisabled();
    await shiftClick(page, 1000);
    await expect(page.locator('#at-multi-count')).toHaveText('3 selected');
    await shot(page, '03-selected');
    // Align at the first: all three at 400, in their order; undo, redo, undo.
    await page.locator('#at-align-first').click();
    expect(await times(page)).toEqual([0, 400, 400, 400]);
    await expect(status(page)).toHaveText('Aligned 3 actions at 0.400 s; they fire in the order they now sit in the list.');
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    await page.keyboard.press('ControlOrMeta+Shift+z');
    expect(await times(page)).toEqual([0, 400, 400, 400]);
    await page.locator('#at-undo').click();
    expect(await times(page)).toEqual(all0);
    // At the last, and at the cursor. Undo keeps the selection; Escape clears it.
    expect(await selected(page)).toEqual([400, 800, 1000]);
    await page.locator('#at-align-last').click();
    expect(await times(page)).toEqual([0, 1000, 1000, 1000]);
    await page.keyboard.press('ControlOrMeta+z');
    await page.keyboard.press('Escape');
    expect(await selected(page)).toEqual([]);
    await page.evaluate(() => { _at.cursorMs = 1500; _atRender(); });
    await shiftClick(page, 0); await shiftClick(page, 400);
    await page.locator('#at-align-cursor').click();
    expect(await times(page)).toEqual([1500, 1500, 800, 1000]);
    await page.keyboard.press('ControlOrMeta+z');
    await page.keyboard.press('Escape');
    // Space evenly 0 / 800 / 1000 -> 0 / 500 / 1000.
    await shiftClick(page, 0); await shiftClick(page, 800); await shiftClick(page, 1000);
    await page.locator('#at-spread').click();
    expect(await times(page)).toEqual([0, 400, 500, 1000]);
    // The arrow keys move the selection, never under 0.
    await page.keyboard.press('ArrowRight');
    expect(await selected(page)).toEqual([10, 510, 1010]);
    await page.keyboard.press('Shift+ArrowRight');
    expect(await selected(page)).toEqual([110, 610, 1110]);
    await page.keyboard.press('Alt+ArrowLeft');
    expect(await selected(page)).toEqual([109, 609, 1109]);
    await page.keyboard.press('Shift+ArrowLeft'); await page.keyboard.press('Shift+ArrowLeft');
    expect(await selected(page)).toEqual([0, 500, 1000]);
    for (let i = 0; i < 6; i++) await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    await page.keyboard.press('Escape');
    // Delete removes the selection; one undo brings it back.
    await shiftClick(page, 400); await shiftClick(page, 1000);
    await page.keyboard.press('Delete');
    expect(await times(page)).toEqual([0, 800]);
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    expect(await selected(page)).toEqual([]);
    // Dragging 400 to 4 px short of 800 sticks to it, a guide drawn meanwhile; one undo step.
    let guides = -1;
    await dragStep(page, 400, (await xOf(page, 800)) - 4, { during: async () => { guides = await page.locator('#at-svg .at-guide').count(); } });
    expect(guides).toBe(1);
    await expect(page.locator('#at-svg .at-guide')).toHaveCount(0);
    expect(await times(page)).toEqual([0, 800, 800, 1000]);
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    // With Alt it lands to the ms; dragged left of 0 it stops at 0.
    const px = await page.evaluate(() => _at.pxPerMs);
    await dragStep(page, 400, (await xOf(page, 800)) - 4, { alt: true });
    const moved = (await times(page))[1];
    expect(Math.abs(moved - (800 - 4 / px))).toBeLessThanOrEqual(2);
    await page.keyboard.press('ControlOrMeta+z');
    await dragStep(page, 400, (await xOf(page, 0)) - 60, { alt: true });
    expect(await times(page)).toEqual([0, 0, 800, 1000]);
    await page.keyboard.press('ControlOrMeta+z');
    // A click edits the step: its time is its delay.
    await (await stepAt(page, 400)).locator('.at-ev-hit').click({ position: { x: 6, y: 10 } });
    await expect(page.locator('#at-step-modal')).toHaveClass(/open/);
    await expect(page.locator('#at-step-title')).toHaveText('Edit action');
    await expect(page.locator('#at-step-type')).toHaveValue('mp3');
    await expect(page.locator('#at-step-time')).toHaveValue('400');
    await page.locator('#at-step-time').fill('450');
    await page.locator('#at-step-apply').click();
    await expect(page.locator('#at-step-modal')).not.toHaveClass(/open/);
    expect(await times(page)).toEqual([0, 450, 800, 1000]);
    // A click on the MP3 lane's empty space starts an MP3 action there.
    const laneY = await page.evaluate(() => {
      const n = document.querySelector('#at-svg .at-lane-name[data-lane="A"]');
      return document.getElementById('at-svg').getBoundingClientRect().top + +n.getAttribute('y') - 4;
    });
    await page.mouse.click(await xOf(page, 1500), laneY);   // at the cursor's time: the cursor line lets it through
    await expect(page.locator('#at-step-title')).toHaveText('New action');
    await expect(page.locator('#at-step-type')).toHaveValue('mp3');
    expect(Math.abs(+(await page.locator('#at-step-time').inputValue()) - 1500)).toBeLessThanOrEqual(3);
    await page.locator('#at-step-time').fill('1500');
    await page.locator('#at-step-apply').click();
    expect(await times(page)).toEqual([0, 450, 800, 1000, 1500]);
    await expect(page.locator('#at-count')).toHaveText('5 of 10 actions');
    // Move the Maestro step before the MP3 one, then Apply: the rows come back in time order with their delays.
    await (await stepAt(page, 800)).locator('.at-ev-hit').click({ position: { x: 6, y: 10 } });
    await page.locator('#at-step-time').fill('300');
    await page.locator('#at-step-apply').click();
    await shot(page, '04-edited');
    await page.locator('#at-apply').click();
    await expect(page.locator('#at-modal')).not.toHaveClass(/open/);
    expect(await rowsOf(page, 1)).toEqual([
      { type: 'wcb_broadcast', cmd: ';W2;S1F0025' },
      { type: 'wcb_unicast', target: '1', cmd: ';M11', delay: 300 },
      { type: 'mp3', fn: 1, track: 5, delay: 450 },
      { type: 'wcb_unicast', target: '1', cmd: ';C03', delay: 1000 },
      expect.objectContaining({ type: 'mp3', delay: 1500 }),
    ]);
    await expect(page.locator('#delay-1-1')).toHaveValue('300');
    // The button's Apply saves them.
    await page.locator('#modal .modal-footer .btn-success').click();
    await expect.poll(() => emu.requests('SET_CONFIG').length).toBeGreaterThan(0);
    expect(await page.evaluate(() => config.mappings['112'].t1.map((a) => a.delay || 0))).toEqual([0, 300, 450, 1000, 1500]);
    // Cancel with changes asks; declined, the timeline stays.
    await openButton(page, 12);
    await openTimeline(page, 1);
    await page.keyboard.press('Escape');   // nothing selected, nothing changed: closes without asking
    await expect(page.locator('#at-modal')).not.toHaveClass(/open/);
    await openTimeline(page, 1);
    await shiftClick(page, 0);
    await page.keyboard.press('Delete');
    answers.push(false);
    await page.locator('#at-cancel').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual('Discard the timeline\'s changes?');
    await expect(page.locator('#at-modal')).toHaveClass(/open/);
    answers.push(true);
    await page.locator('#at-cancel').click();
    await expect(page.locator('#at-modal')).not.toHaveClass(/open/);
    expect(await rowsOf(page, 1)).toHaveLength(5);
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_library the command library sits beside the timeline (hidden and shown again, remembered); a search opens what matches; a command with no fields lands at the cursor at once, routed as a new action row would route it, in its device\'s lane and selected; one with fields opens the composer, whose button says where it lands; a device string goes to the WCB port labelled for it; a command dragged onto the timeline lands where it is dropped; Cancel adds nothing; a full tap refuses more', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await openButton(page, 12, { exclusive: false, t1: [{ type: 'wcb_unicast', target: '1', cmd: ';M11', delay: 800 }] });
    await openTimeline(page, 1);
    await expect(page.locator('#at-libpanel')).toBeVisible();
    await page.locator('#at-lib-btn').click();
    await expect(page.locator('#at-libpanel')).toBeHidden();
    expect(await page.evaluate(() => localStorage.getItem('nc_at_lib_open'))).toBe('0');
    await page.locator('#at-lib-btn').click();
    await expect(page.locator('#at-libpanel')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('nc_at_lib_open'))).toBe('1');
    await expect(libCmd(page, 'mp3.stop')).toBeHidden();
    await page.locator('#at-lib-search').fill('toggle');
    await expect(libCmd(page, 'mp3.stop')).toBeVisible();
    await shot(page, '05-library-search');
    // No fields: straight in at the cursor, to the MP3 Trigger's configured home (config.mp3Dest: WCB 2).
    await page.evaluate(() => { _at.cursorMs = 300; _atRender(); });
    await libCmd(page, 'mp3.stop').click();
    expect(await page.evaluate(() => _at.events.find((e) => e.t === 300).action)).toEqual({ type: 'wcb_unicast', target: '2', cmd: ';A,STOP' });
    expect(await selected(page)).toEqual([300]);
    await expect(status(page)).toHaveText('Added “Start/Stop Toggle” at 0.300 s — MP3 Trigger · via WCB 2. Click it to change where it goes.');
    await expect(await stepAt(page, 300)).toHaveAttribute('data-lane', 'A');
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual([800]);
    // Fields: the composer, labelled with where it lands; Cancel adds nothing.
    await page.locator('#at-lib-search').fill('color projector');
    await libCmd(page, 'flthy.led.colorproj').click();
    await expect(page.locator('#cmdlib-modal')).toHaveClass(/open/);
    await expect(page.locator('#cmdlib-use')).toHaveText('Add at 0.300 s →');
    await page.locator('#cmdlib-cancel2').click();
    expect(await times(page)).toEqual([800]);
    await libCmd(page, 'flthy.led.colorproj').click();
    await page.locator('#cmdlib-body select[data-pname="designator"]').selectOption('R');
    await page.locator('#cmdlib-body select[data-pname="color"]').selectOption('1');
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    // WCB 2 says its Serial 1 has the Flthy HP controller: the string goes out there, in the FlthyHPs lane.
    expect(await page.evaluate(() => _at.events.find((e) => e.t === 300).action)).toEqual({ type: 'wcb_broadcast', cmd: ';W2;S1R0021' });
    await expect(await stepAt(page, 300)).toHaveAttribute('data-lane', 'D:flthy-hps:W2S1');
    await expect((await stepAt(page, 300)).locator('.at-ev-label')).toHaveText('Color Projector · Rear · Red');
    // Dragged onto the timeline: lands where it is dropped (Alt-free placement is not needed; nothing is near 600).
    await page.locator('#at-lib-search').fill('next track');
    const svg = await page.locator('#at-svg').boundingBox();
    await libCmd(page, 'mp3.next').dragTo(page.locator('#at-svg'), { targetPosition: { x: (await xOf(page, 600)) - svg.x, y: svg.height - 12 } });
    const dropped = await page.evaluate(() => _at.events.find((e) => e.action.cmd === ';A,NEXT'));
    expect(Math.abs(dropped.t - 600)).toBeLessThanOrEqual(3);
    await expect(await stepAt(page, dropped.t)).toHaveAttribute('data-lane', 'A');
    await shot(page, '06-library-added');
    // Fill the tap to the board's ten, then the library refuses an eleventh.
    await page.evaluate(() => { for (let i = 0; i < 7; i++) _atAddResolved({ type: 'mp3', fn: 3 }, 1200 + 100 * i); });
    await expect(page.locator('#at-count')).toHaveText('10 of 10 actions');
    await page.locator('#at-lib-search').fill('toggle');
    await libCmd(page, 'mp3.stop').click();
    await expect(page.locator('#at-count')).toHaveText('10 of 10 actions');
    await expect(status(page)).toHaveText('This tap already has 10 actions, as many as the board holds. Delete one to add another.');
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_warn the timeline says where the board would not run the tap as drawn: a delay past the board\'s 30 minutes is an error that blocks Apply until the step is moved; two timed sequences on one WCB overlapping is warned of (the second replaces the first); clip controls are noted as skipped by Test; an incomplete row is said to be left out', async ({ page, emu }) => {
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
    // A blank row on the triple tap (no command yet).
    await page.locator('#tiers-container .tier-tab[data-tier="3"]').click();
    await page.locator('#tier-card-3 .btn-add-action').click();
    await openTimeline(page, 3);
    await expect(page.locator('#at-svg .at-ref')).toHaveCount(2);
    await expect.poll(() => page.locator('#at-svg g.at-ref').evaluateAll((gs) => gs.map((g) => g.dataset.state))).toEqual(['ready', 'ready']);
    await expect(page.locator('#at-warn > div')).toHaveText([
      '✖ “h” has a 2000000 ms delay: this board stores at most 1800000 ms (30:00.000) and would cut it. Move it.',
      '⚠ WCB 1 runs one timed sequence at a time: starting 04 at 1.500 s replaces 03, which runs until 4.500 s.',
      'ℹ ▶ Test skips the clip controls (record, play, stop); a press runs them.',
    ]);
    await expect(status(page)).toHaveText('1 incomplete row is not on the timeline (no command yet); Apply leaves it out, as Save would.');
    await expect(page.locator('#at-apply')).toBeDisabled();
    expect(await page.evaluate(() => _at.events.find((e) => e.action.cmd === ';S1h').t)).toBe(1800000);
    await shot(page, '07-warnings');
    // Moved, the error goes and Apply works; the blank row is gone, the rest are in time order.
    const h = await page.evaluate(() => _at.events.find((e) => e.action.cmd === ';S1h').t);
    await (await stepAt(page, h)).locator('.at-ev-hit').click({ position: { x: 6, y: 10 } });
    await page.locator('#at-step-time').fill('2500');
    await page.locator('#at-step-apply').click();
    await expect(page.locator('#at-warn > div').first()).toHaveText(/^⚠ WCB 1 runs one timed sequence at a time/);
    await expect(page.locator('#at-apply')).toBeEnabled();
    await page.locator('#at-apply').click();
    expect((await rowsOf(page, 3)).map((a) => [a.cmd || a.type, a.delay || 0])).toEqual([['beep', 0], [';C03', 500], [';C04', 1500], [';S1h', 2500]]);
    await expect(page.locator('#tier-card-3 .action-row')).toHaveCount(4);
    T.expectNoPageErrors(page);
  });

  test('nctool.action_timeline_test_switch ▶ Test in the timeline fires its actions as they stand, before Apply, the way a press does: each at its delay after the start (two at one delay together, not one after the other), the cursor running alongside; the tap\'s own ▶ Test does the same; Preview with no board plays the cursor and lights each action as it passes, sending nothing; a switch position has the strip and the timeline too', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    // No board: Preview only.
    await openButton(page, 12, { exclusive: false, t1: [{ type: 'mp3', fn: 1, track: 2, delay: 500 }] });
    await openTimeline(page, 1);
    await page.locator('#at-preview').click();
    await expect(page.locator('#at-preview')).toHaveText('■ Stop');
    await page.clock.runFor(580);
    await expect(page.locator('#at-svg g.at-ev.flash')).toHaveCount(1);
    await shot(page, '08-preview');
    await page.clock.runFor(1500);
    await expect(page.locator('#at-preview')).toHaveText('▶ Preview');
    await expect(page.locator('#at-svg g.at-ev.flash')).toHaveCount(0);
    expect(emu.rx).toEqual([]);
    await page.locator('#at-close').click();
    await page.evaluate(() => closeModal());
    // Connected: Test fires the timeline's edits, not the rows, each at its delay from the start.
    await connect(page, emu);
    const d = (cmd, delay) => ({ type: 'wcb_unicast', target: '1', cmd, ...(delay ? { delay } : {}) });
    await openButton(page, 12, { exclusive: false, t1: [d(';S1a', 1000), d(';S1b', 1000), d(';S1c', 0)] });
    await openTimeline(page, 1);
    await page.evaluate(() => { _at.events.find((e) => e.action.cmd === ';S1c').t = 300; _atRender(); });   // an edit, not applied
    await page.locator('#at-test').click();
    expect(await page.evaluate(() => !!_at.playing)).toBe(true);
    await page.clock.runFor(350);
    await T.waitRequests(emu, 'TEST_ACTION', 1);
    expect(emu.requests('TEST_ACTION').map((m) => m.action.cmd)).toEqual([';S1c']);
    await page.clock.runFor(700);
    await T.waitRequests(emu, 'TEST_ACTION', 3);
    expect(emu.requests('TEST_ACTION').map((m) => m.action.cmd)).toEqual([';S1c', ';S1a', ';S1b']);
    expect(emu.requests('TEST_ACTION').every((m) => m.action.delay === undefined)).toBe(true);
    expect(await rowsOf(page, 1)).toEqual([d(';S1a', 1000), d(';S1b', 1000), d(';S1c', 0)]);
    await page.clock.runFor(1500);
    // The tap's own ▶ Test: the same rule, from the rows (a and b at 1 s together, c at once). The edit was never
    // applied, so the timeline closes without asking.
    await page.locator('#at-cancel').click();
    await expect(page.locator('#at-modal')).not.toHaveClass(/open/);
    await page.locator('#tier-card-1 button.btn-ghost', { hasText: '▶ Test' }).click();   // the tap's, not a row's
    await T.waitRequests(emu, 'TEST_ACTION', 4);
    expect(emu.requests('TEST_ACTION')[3].action.cmd).toBe(';S1c');
    await page.clock.runFor(1050);
    await T.waitRequests(emu, 'TEST_ACTION', 6);
    expect(emu.requests('TEST_ACTION').slice(3).map((m) => m.action.cmd)).toEqual([';S1c', ';S1a', ';S1b']);
    await page.evaluate(() => closeModal());
    // A switch position: the strip and the timeline, Apply into its rows.
    await page.locator('.switch-zone[data-sw="SB"]:visible').first().click();
    await expect(page.locator('#at-open-p0')).toBeVisible();
    await page.locator('#fields-p0-0-cmd').fill(';S3HILSW');
    await page.locator('#delay-p0-0').fill('250');
    await page.clock.runFor(200);
    expect(await page.locator('#at-strip-p0 .at-strip-dot').evaluateAll((cs) => cs.map((c) => +c.dataset.t))).toEqual([250]);
    await page.locator('#at-open-p0').click();
    await expect(page.locator('#at-modal')).toHaveClass(/open/);
    await expect(page.locator('#at-title')).toHaveText(/^⏱ Switch SB · Down/);
    expect(await times(page)).toEqual([250]);
    await page.keyboard.press('Escape');
    T.expectNoPageErrors(page);
  });
});

test.describe('the action timeline on a board whose firmware predates the limits', () => {
  test.use({ emuOptions: { config: BENCH, roster: ROSTER, seqs: SEQS, legacyLimits: true } });

  test('nctool.action_timeline_legacy_board a board on firmware from before ten actions a tier sends no limits, so the tool holds to the old ones: five actions a tap (+ Add action and the timeline stop there), a delay to 65535 ms (past it the timeline says the board would wrap it), and eight pending slots (more in one press is warned of, the rest firing at once); Save refuses a config the board would cut and sends nothing; a board on current firmware lifts them', async ({ page, emu }) => {
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
    await openTimeline(page, 1);
    await expect(page.locator('#at-count')).toHaveText('5 of 5 actions');
    await page.locator('#at-add').click();
    await expect(page.locator('#at-step-modal')).not.toHaveClass(/open/);
    await expect(status(page)).toHaveText('This tap already has 5 actions, as many as the board holds. Delete one to add another.');
    await page.locator('#at-close').click();
    await page.evaluate(() => closeModal());
    // A triple tap queuing 10 delayed actions with the taps that fire with it, one of them past 65535 ms.
    await openButton(page, 12, {
      exclusive: false,
      t1: [d(';S1a', 100), d(';S1b', 200), d(';S1c', 300), d(';S1d', 400)],
      t2: [d(';S1e', 100), d(';S1f', 200), d(';S1g', 300)],
      t3: [d(';S1h', 500), d(';S1i', 600), d(';S1j', 70000)],
    });
    await openTimeline(page, 3);
    await expect(page.locator('#at-warn > div')).toHaveText([
      '✖ “j” has a 70000 ms delay: this board stores at most 65535 ms (1:05.535) and would wrap it. Move it.',
      '⚠ This press queues 10 delayed actions with the taps that fire with it: the board holds 8 at once, shared with every other button and switch, and fires the rest at once, their delays dropped (PENDING_ACTION_SLOTS; firmware before the 32 slots).',
    ]);
    await expect(page.locator('#at-apply')).toBeDisabled();
    await shot(page, '09-legacy-board');
    await page.locator('#at-close').click();
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
