// The timeline editor's action lanes (NaviCore docs/CONFIG_TOOL.md §7): a clip's actions in a lane per device they
// reach, in the WCB Wizard's device colours (_tlLaneOf, _tlColorOf, config_tool/index.html), the sequence an action
// runs drawn under it from GET_WCB_SEQVAL (_tlRecallOf), selecting several and aligning or spacing them, moving with
// sticking and the arrow keys (_tlStartDragAction, _tlSnap, _tlNudgeSel), the command library docked beside the
// timeline (_tlLibRender, _tlLibPick, _cmdlibResolveAction), a new clip built without recording (clipNewTimeline), and
// Preview with no board. The emulator's clip store serves the clips as clips.spec.js does. TL_SHOTS=<dir> saves a
// screenshot of the editor at each step (TL_THEME=light for the light theme).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const { normEvent } = require('../../lib/navicore/clips');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
// Two boards: WCB 2 says its Serial 1 has the Flthy HP controller (WDP port labels, WCB_STATUS portLabels).
const ROSTER = { online: [1, 1], known: [1, 1], clients: [0, 0], temporary: [0, 0], aliases: ['', 'Dome'],
                 portLabels: [['', '', '', '', ''], ['Flthy HP Controller', '', '', '', '']], seqHash: [0, 0] };
const SEQS = { 1: { '03': ';m11^;t4000^;w2testing', long: ';A,PLAY,1^;t250^;A,STOP' } };

// "show": one servo track (control points at 0 and 2000), and an action of each kind of lane.
const SHOW = () => [
  { t: 0, k: 1, slot: 1, ch: 0, pos: 6000 }, { t: 2000, k: 1, slot: 1, ch: 0, pos: 7000 },
  { t: 200, k: 0, type: 'wcb_broadcast', cmd: ';W2;S1F0025' },          // FlthyHPs: Color Projector, Front, Blue
  { t: 400, k: 0, type: 'wcb_unicast', target: '2', cmd: ';S1wave' },    // text the library does not know, same port
  { t: 600, k: 0, type: 'mp3', fn: 1, track: 5 },
  { t: 800, k: 0, type: 'wcb_unicast', target: '1', cmd: ';M11' },
  { t: 1000, k: 0, type: 'wcb_unicast', target: '1', cmd: ';C03' },      // runs sequence 03 on WCB 1
  { t: 1500, k: 0, type: 'play', cmd: 'beep', fn: 0 },
].sort((a, b) => a.t - b.t).map(normEvent);   // stored in time order, as the board keeps a clip
const CLIPS = () => ({ show: { mode: 1, events: SHOW() } });

const SHOTS = process.env.TL_SHOTS || '';
async function shot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.locator('#tl-modal .modal').screenshot({ path: path.join(SHOTS, name + '.png') });
}
async function theme(page) {
  if (process.env.TL_THEME === 'light') await page.evaluate(() => document.body.classList.add('theme-light'));
}
const rowButton = (page, name, label) =>
  page.locator('#clips-list > div').filter({ has: page.getByText(name, { exact: true }) }).locator('button', { hasText: label });
// Connected, the 3 s WCB status poll has brought the roster: aliases and WCB 2's port labels.
async function connect(page, emu) {
  await T.connectUsb(page, emu);
  await page.clock.fastForward(3100);
  await expect.poll(() => page.evaluate(() => (typeof wcbPortLabels !== 'undefined' && wcbPortLabels[1]) ? wcbPortLabels[1][0] : '')).toBe('Flthy HP Controller');
}
async function openClip(page, name, status) {
  await page.locator('#btn-clips').click();
  await expect(page.locator('#clips-modal')).toHaveClass(/open/);
  await rowButton(page, name, '📈').click();
  await expect(page.locator('#tl-modal')).toHaveClass(/open/);
  await expect(page.locator('#tl-status')).toHaveText(status, { timeout: 20_000 });
}
// The lanes as drawn, top to bottom: [name, sub].
const lanes = (page) => page.evaluate(() => [...document.querySelectorAll('#tl-svg .tl-lane-name')].map((n) => {
  const sub = n.nextElementSibling && n.nextElementSibling.classList.contains('tl-lane-sub') ? n.nextElementSibling.textContent : '';
  return [n.dataset.lane, n.querySelector('title').textContent.split(' — ')[0], sub];
}));
const actions = (page) => page.evaluate(() => _tlClip.actions.map((x) => [x.t, x.action.type, x.action.cmd || '']));
const times = (page) => page.evaluate(() => _tlClip.actions.map((x) => x.t));
const selected = (page) => page.evaluate(() => _tlClip.actions.filter((x) => _tlClip.selActs.has(x.id)).map((x) => x.t));
// A step's group, found by its action's time (no two of a clip's actions share one).
const stepAt = (page, t) => page.evaluate((tt) => _tlClip.actions.find((x) => x.t === tt).id, t)
  .then((id) => page.locator(`#tl-svg g.tl-ev[data-act="${id}"]`));
async function shiftClick(page, t) { await (await stepAt(page, t)).locator('.tl-ev-hit').click({ modifiers: ['Shift'], position: { x: 6, y: 10 } }); }
// The page x of time t on the timeline.
const xOf = (page, t) => page.evaluate((tt) => document.getElementById('tl-svg').getBoundingClientRect().left + _tlTimeToX(tt), t);
// Drag the step at time t by its dot to page x `toX`; `during` runs before the button is let go.
async function dragStep(page, t, toX, { alt = false, during = null } = {}) {
  const b = await (await stepAt(page, t)).locator('.tl-ev-dot').boundingBox();
  const y = b.y + b.height / 2;
  await page.mouse.move(b.x + b.width / 2, y);
  await page.mouse.down();
  if (alt) await page.keyboard.down('Alt');
  await page.mouse.move(toX, y, { steps: 6 });
  if (during) await during();
  await page.mouse.up();
  if (alt) await page.keyboard.up('Alt');
}
const status = (page) => page.locator('#tl-status');
const libCmd = (page, id) => page.locator(`#tl-lib-list .tl-lib-cmd[data-cmd="${id}"]`);

test.describe('the timeline\'s action lanes', () => {
  test.use({ emuOptions: { config: BENCH, clips: CLIPS(), roster: ROSTER, seqs: SEQS } });

  test('nctool.timeline_action_lanes each action draws in the lane of the device it reaches, named with where it goes: a device string the library knows in its device\'s lane and unknown text on the same port in the port\'s (labelled from WDP), a WCB verb in its device\'s lane, the native MP3 action in the MP3 lane, a clip control in Clips; lanes in device order, each in its board\'s home colour and no two alike; a step labelled with what it does; the sequence an action runs is read from its board and drawn under it, its steps where they land in their devices\' colours, its run hatched and the view carried past the clip\'s end; a broadcast run waits for a list that names its board, a key the board lacks says so', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await openClip(page, 'show', '8 events · 2.0 s');
    expect(await lanes(page)).toEqual([
      ['clips', 'Clips', 'this controller'],
      ['C', 'Sequences', 'via WCB 1'],
      ['M1', 'Maestro 1', 'via WCB 1'],
      ['A', 'MP3 Trigger', ''],
      ['D:flthy-hps:W2S1', 'FlthyHPs Holoprojectors', 'WCB 2 · Dome · Serial 1'],
      ['S:W2S1', 'Serial 1 · Flthy HP Controller', 'WCB 2 · Dome'],
    ]);
    // Home colours: Maestro 1, MP3 2, Sequences 3, FlthyHPs 8, the NaviCore board's clip controls 6. The port lane
    // has no board and takes the first free one (after every lane with a home has picked), and the "Text to WCB 2"
    // only sequence 03 reaches takes the next.
    expect(await page.evaluate(() => Object.fromEntries(_tlClip.colors))).toEqual({
      'D:flthy-hps:W2S1': 7, A: 1, M1: 0, C: 2, clips: 5, 'S:W2S1': 3, 'T:2': 4,
    });
    const swatches = await page.evaluate(() => [...document.querySelectorAll('#tl-svg .tl-lane-name')].map((n) => n.previousElementSibling.getAttribute('fill')));
    expect(swatches).toEqual(['var(--tl-c6)', 'var(--tl-c3)', 'var(--tl-c1)', 'var(--tl-c2)', 'var(--tl-c8)', 'var(--tl-c4)']);
    const labels = await page.evaluate(() => [...document.querySelectorAll('#tl-svg g.tl-ev')].map((g) => [g.dataset.lane, g.querySelector('.tl-ev-label').textContent]));
    expect(labels).toEqual([
      ['clips', 'Play "beep"'], ['C', 'Run Sequence (short) · 03'], ['M1', 'Run subroutine · 1'], ['A', 'Play track 5'],
      ['D:flthy-hps:W2S1', 'Color Projector · Front · Blue'], ['S:W2S1', 'wave'],
    ]);
    // Sequence 03 (;m11^;t4000^;w2testing), pulled from WCB 1 and drawn from the ;C03 at 1 s: its steps at 1 s and 5 s.
    expect(emu.requests('GET_WCB_SEQVAL').map((m) => [m.wcb, m.key])).toEqual([[1, '03']]);
    const ref = page.locator('#tl-svg g.tl-ref');
    await expect(ref).toHaveAttribute('data-state', 'ready');
    expect(await ref.locator('.tl-ref-step').evaluateAll((cs) => cs.map((c) => [+c.dataset.t, c.getAttribute('fill')])))
      .toEqual([[1000, 'var(--tl-c1)'], [5000, 'var(--tl-c5)']]);
    expect(await ref.locator('.tl-ref-step title').allTextContents()).toEqual([
      '+0.000 s (1.000 s in the clip) · ;m11 · Maestro 1', '+4.000 s (5.000 s in the clip) · ;w2testing · Text to WCB 2 · Dome']);
    expect(+(await ref.locator('.tl-ref-span').getAttribute('width'))).toBeCloseTo(4000 * (await page.evaluate(() => _tlClip.pxPerMs)), 3);
    expect(await page.locator('#tl-svg .tl-ref-name').evaluate((n) => n.firstChild.textContent)).toBe('↳ 03 · WCB 1');
    // The view runs on to the sequence's last step; past the clip's end it is shaded.
    expect(+(await page.locator('#tl-svg').getAttribute('width'))).toBeCloseTo(150 + 5000 * (await page.evaluate(() => _tlClip.pxPerMs)) + 30, 3);
    await expect(page.locator('#tl-svg .tl-after-end')).toHaveCount(1);
    await page.locator('#tl-zoom-fit').click();
    await shot(page, '01-lanes');
    // A broadcast ;Clong: no pulled list names a board for it, so nothing is asked; once the names are in, WCB 1's
    // copy is read and drawn. A key WCB 1 does not have says so.
    await page.evaluate(() => {
      _tlClip.actions.push({ id: _tlId(), t: 1800, action: { type: 'wcb_broadcast', cmd: ';Clong' } },
                           { id: _tlId(), t: 1900, action: { type: 'wcb_unicast', target: '1', cmd: ';C99' } },
                           { id: _tlId(), t: 1950, action: { type: 'wcb_unicast', target: '2', cmd: ';C03' } });
      _tlRender();
    });
    // WCB 2 holds no sequence store in the emulator: the RC answers ok:false, and the row says nothing came back.
    const refState = () => page.locator('#tl-svg g.tl-ref').evaluateAll((gs) => gs.map((g) => g.dataset.state));
    await expect.poll(refState).toEqual(['ready', 'nowhere', 'missing', 'noanswer']);
    await expect(page.locator('#tl-svg g.tl-ref .tl-ref-note')).toHaveText([
      'on every board that has it (refresh a sequence list to see it)', 'WCB 1 has no sequence 99',
      'no answer from WCB 2 (reopen the clip to ask again)']);
    await page.evaluate(() => _wcbSeqRefresh(null, true));
    await page.evaluate(() => _tlRender());
    await expect.poll(refState).toEqual(['ready', 'ready', 'missing', 'noanswer']);
    expect(await page.locator('#tl-svg g.tl-ref').nth(1).locator('.tl-ref-step').evaluateAll((cs) => cs.map((c) => [+c.dataset.t, c.getAttribute('fill')])))
      .toEqual([[1800, 'var(--tl-c2)'], [2050, 'var(--tl-c2)']]);
    expect(emu.requests('GET_WCB_SEQVAL').map((m) => [m.wcb, m.key])).toEqual([[1, '03'], [1, '99'], [2, '03'], [1, 'long']]);
    T.expectNoPageErrors(page);
  });

  test('nctool.timeline_action_select_move Shift-click selects several steps and shows Align and Space evenly; Align puts them at the first\'s, the last\'s or the cursor\'s time 5 ms apart in their order, Space evenly keeps the ends and evens the middle (5 ms past an action already on its time); the arrow keys nudge the selection 10 / 100 / 1 ms; Delete removes all of them in one undo step; Escape clears the selection; a dragged step sticks to the cursor, a servo control point or another step with a guide drawn, Alt places it to the ms, dragging a selected step moves the selection, and a drag is one undo step; a click on a lane\'s empty space starts an action of that lane\'s kind', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await openClip(page, 'show', '8 events · 2.0 s');
    const all0 = await times(page);
    expect(all0).toEqual([200, 400, 600, 800, 1000, 1500]);
    await shiftClick(page, 200);
    await shiftClick(page, 400);
    await expect(page.locator('#tl-multi-toolbar')).toBeVisible();
    await expect(page.locator('#tl-spread')).toBeDisabled();
    await shiftClick(page, 600);
    await expect(page.locator('#tl-multi-count')).toHaveText('3 selected');
    await expect(page.locator('#tl-spread')).toBeEnabled();
    expect(await selected(page)).toEqual([200, 400, 600]);
    await shot(page, '02-selected');
    // Align: first, then undo; cursor; last.
    await page.locator('#tl-align-first').click();
    expect(await times(page)).toEqual([200, 205, 210, 800, 1000, 1500]);
    await expect(status(page)).toHaveText('Aligned 3 actions at 0.200 s, 5 ms apart in their order (two actions never share a millisecond).');
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    await page.evaluate(() => { _tlClip.cursorMs = 1200; _tlRender(); });
    await shiftClick(page, 200); await shiftClick(page, 400); await shiftClick(page, 600);
    await page.locator('#tl-align-cursor').click();
    expect(await times(page)).toEqual([800, 1000, 1200, 1205, 1210, 1500]);
    await page.keyboard.press('ControlOrMeta+z');
    await shiftClick(page, 200); await shiftClick(page, 400); await shiftClick(page, 600);
    await page.locator('#tl-align-last').click();
    expect(await times(page)).toEqual([590, 595, 600, 800, 1000, 1500]);
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    // Space evenly 200 / 400 / 1000: the middle goes to 600, where the MP3 action is, so 5 ms past it.
    await shiftClick(page, 200); await shiftClick(page, 400); await shiftClick(page, 1000);
    await page.locator('#tl-spread').click();
    expect(await times(page)).toEqual([200, 600, 605, 800, 1000, 1500]);
    expect(await selected(page)).toEqual([200, 605, 1000]);
    // The arrow keys move the selection; each press is one undo step.
    await page.keyboard.press('ArrowRight');
    expect(await selected(page)).toEqual([210, 615, 1010]);
    await page.keyboard.press('Shift+ArrowRight');
    expect(await selected(page)).toEqual([310, 715, 1110]);
    await page.keyboard.press('Alt+ArrowLeft');
    expect(await selected(page)).toEqual([309, 714, 1109]);
    for (let i = 0; i < 4; i++) await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    // Delete takes every selected action; one undo brings them all back.
    await shiftClick(page, 400); await shiftClick(page, 800);
    await page.keyboard.press('Delete');
    expect(await times(page)).toEqual([200, 600, 1000, 1500]);
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    await shiftClick(page, 400);
    await page.keyboard.press('Escape');
    expect(await selected(page)).toEqual([]);
    await expect(page.locator('#tl-multi-toolbar')).toBeHidden();
    // Dragging: 800 dropped 4 px short of the cursor (1200) sticks to it, a guide drawn while it does.
    let guides = -1;
    await dragStep(page, 800, (await xOf(page, 1200)) - 4, { during: async () => { guides = await page.locator('#tl-svg .tl-guide').count(); } });
    expect(guides).toBe(1);
    await expect(page.locator('#tl-svg .tl-guide')).toHaveCount(0);
    expect(await times(page)).toEqual([200, 400, 600, 1000, 1200, 1500]);
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    // 1500 dropped 5 px past the servo's control point at 2000 sticks to it.
    await dragStep(page, 1500, (await xOf(page, 2000)) + 5);
    expect(await times(page)).toContain(2000);
    await page.keyboard.press('ControlOrMeta+z');
    // With Alt it lands to the ms where it was dropped, with no guide.
    const px = await page.evaluate(() => _tlClip.pxPerMs);
    await dragStep(page, 800, (await xOf(page, 1200)) - 4, { alt: true, during: async () => { guides = await page.locator('#tl-svg .tl-guide').count(); } });
    expect(guides).toBe(0);
    const t = (await times(page)).find((v) => !all0.includes(v));
    expect(Math.abs(t - (1200 - 4 / px))).toBeLessThanOrEqual(2);
    await page.keyboard.press('ControlOrMeta+z');
    // A selected step dragged moves the selection with it.
    await shiftClick(page, 200); await shiftClick(page, 400);
    await dragStep(page, 400, (await xOf(page, 500)) + 0, { alt: true });
    const moved = await selected(page);
    expect(moved[1] - moved[0]).toBe(200);
    expect(Math.abs(moved[1] - 500)).toBeLessThanOrEqual(2);
    await page.keyboard.press('ControlOrMeta+z');
    expect(await times(page)).toEqual(all0);
    // A click (no drag) on a step edits it; on a lane's empty space, starts one of the lane's kind.
    await (await stepAt(page, 600)).locator('.tl-ev-hit').click({ position: { x: 6, y: 10 } });
    await expect(page.locator('#tl-action-modal')).toHaveClass(/open/);
    await expect(page.locator('#tl-action-title')).toHaveText('Edit action');
    await expect(page.locator('#tl-action-type')).toHaveValue('mp3');
    await page.locator('#tl-action-close').click();
    const band = await page.evaluate(() => {
      const n = document.querySelector('#tl-svg .tl-lane-name[data-lane="A"]');
      const r = document.getElementById('tl-svg').getBoundingClientRect();
      return { y: r.top + +n.getAttribute('y') - 4 };
    });
    await page.mouse.click(await xOf(page, 1700), band.y);
    await expect(page.locator('#tl-action-title')).toHaveText('New action');
    await expect(page.locator('#tl-action-type')).toHaveValue('mp3');
    await page.locator('#tl-action-close').click();
    T.expectNoPageErrors(page);
  });

  test('nctool.timeline_library_add the command library sits beside the timeline (hidden and shown again, remembered); a search opens what matches; a command with no fields is added at the cursor at once, routed as a new action row would route it, in its device\'s lane and selected; one with fields opens the composer, whose button says where it lands; a device string goes to the WCB port labelled for it; a command dragged onto the timeline lands where it is dropped; a cancelled composer adds nothing; an add is one undo step', async ({ page, emu }) => {
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await openClip(page, 'show', '8 events · 2.0 s');
    await expect(page.locator('#tl-libpanel')).toBeVisible();
    await page.locator('#tl-lib-btn').click();
    await expect(page.locator('#tl-libpanel')).toBeHidden();
    expect(await page.evaluate(() => localStorage.getItem('nc_tl_lib_open'))).toBe('0');
    await page.locator('#tl-lib-btn').click();
    await expect(page.locator('#tl-libpanel')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('nc_tl_lib_open'))).toBe('1');
    // Collapsed until a search opens what matches.
    await expect(libCmd(page, 'mp3.stop')).toBeHidden();
    await page.locator('#tl-lib-search').fill('toggle');
    await expect(libCmd(page, 'mp3.stop')).toBeVisible();
    await shot(page, '03-library-search');
    // No fields: straight in at the cursor, to the MP3 Trigger's configured home (config.mp3Dest: WCB 2).
    await page.evaluate(() => { _tlClip.cursorMs = 1700; _tlRender(); });
    await libCmd(page, 'mp3.stop').click();
    expect(await actions(page)).toContainEqual([1700, 'wcb_unicast', ';A,STOP']);
    expect(await page.evaluate(() => _tlClip.actions.find((x) => x.t === 1700).action)).toEqual({ type: 'wcb_unicast', target: '2', cmd: ';A,STOP' });
    expect(await selected(page)).toEqual([1700]);
    await expect(status(page)).toHaveText('Added “Start/Stop Toggle” at 1.700 s — MP3 Trigger · via WCB 2. Click it to change where it goes.');
    await expect((await stepAt(page, 1700))).toHaveAttribute('data-lane', 'A');
    await page.keyboard.press('ControlOrMeta+z');
    expect((await times(page)).includes(1700)).toBe(false);
    // Fields: the composer, labelled with where it lands (1.700 s is free again); Cancel adds nothing.
    await page.locator('#tl-lib-search').fill('color projector');
    await libCmd(page, 'flthy.led.colorproj').click();
    await expect(page.locator('#cmdlib-modal')).toHaveClass(/open/);
    await expect(page.locator('#cmdlib-use')).toHaveText('Add at 1.700 s →');
    await page.locator('#cmdlib-cancel2').click();
    expect(await times(page)).toEqual([200, 400, 600, 800, 1000, 1500]);
    await libCmd(page, 'flthy.led.colorproj').click();
    await page.locator('#cmdlib-body select[data-pname="designator"]').selectOption('R');
    await page.locator('#cmdlib-body select[data-pname="color"]').selectOption('1');
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#cmdlib-modal')).not.toHaveClass(/open/);
    // WCB 2 says its Serial 1 has the Flthy HP controller: the string goes out there, in the FlthyHPs lane.
    expect(await page.evaluate(() => _tlClip.actions.find((x) => x.t === 1700).action)).toEqual({ type: 'wcb_broadcast', cmd: ';W2;S1R0021' });
    await expect((await stepAt(page, 1700))).toHaveAttribute('data-lane', 'D:flthy-hps:W2S1');
    await expect((await stepAt(page, 1700)).locator('.tl-ev-label')).toHaveText('Color Projector · Rear · Red');
    // Dragged onto the timeline: lands where it is dropped (on any lane; its lane is its device).
    await page.locator('#tl-lib-search').fill('next track');
    const svg = await page.locator('#tl-svg').boundingBox();
    await libCmd(page, 'mp3.next').dragTo(page.locator('#tl-svg'), { targetPosition: { x: (await xOf(page, 300)) - svg.x, y: svg.height - 12 } });
    const dropped = await page.evaluate(() => _tlClip.actions.find((x) => x.action.cmd === ';A,NEXT'));
    expect(Math.abs(dropped.t - 300)).toBeLessThanOrEqual(3);
    await expect((await stepAt(page, dropped.t))).toHaveAttribute('data-lane', 'A');
    await shot(page, '04-library-added');
    T.expectNoPageErrors(page);
  });

  test('nctool.timeline_new_clip ＋ New clip asks a name, offers to open an existing clip of that name instead, and otherwise opens an empty timeline under the name the board would use (only letters, digits, _ and -); an empty one refuses to save; built from the library it saves through EDITBEGIN / EDITEV / EDITEND under that name and lands in the list', async ({ page, emu }) => {
    const answers = [];
    const dialogs = T.answerDialogs(page, answers);
    await T.openTool(page);
    await theme(page);
    await connect(page, emu);
    await page.locator('#btn-clips').click();
    await expect(page.locator('#clips-modal')).toHaveClass(/open/);
    await expect(rowButton(page, 'show', '📈')).toBeVisible({ timeout: 20_000 });
    // A name the droid already has: offered the existing one; declined, nothing opens.
    answers.push('show', false);
    await page.locator('#clip-new-btn').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual('The droid already has a clip named "show". Open it in the timeline instead?');
    await expect(page.locator('#tl-modal')).not.toHaveClass(/open/);
    answers.push('my show!');
    await page.locator('#clip-new-btn').click();
    await expect(page.locator('#tl-modal')).toHaveClass(/open/);
    await expect(page.locator('#tl-title')).toHaveText('📈 Timeline — myshow (new)');
    await expect(status(page)).toHaveText('New clip — add actions from the library, or + Maestro track for servo moves, then Save. Nothing is on the droid until you do.');
    await expect(page.locator('#tl-svg .tl-lane-new')).toHaveText('Actions');
    await expect(page.locator('#tl-length')).toHaveValue('10.0');
    await shot(page, '05-new-clip');
    await page.locator('#tl-save-btn').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual('Nothing to save — the timeline is empty.');
    expect(emu.clips.has('myshow')).toBe(false);
    // Two actions from the library, then Save.
    await page.locator('#tl-lib-search').fill('next track');
    await libCmd(page, 'mp3.next').click();
    await page.evaluate(() => { _tlClip.cursorMs = 1500; _tlRender(); });
    await page.locator('#tl-lib-search').fill('toggle');
    await libCmd(page, 'mp3.stop').click();
    await page.locator('#tl-save-btn').click();
    await expect(status(page)).toHaveText('Saved ✓ (2 events)', { timeout: 20_000 });
    await expect(page.locator('#tl-title')).toHaveText('📈 Timeline — myshow');
    expect(emu.clips.get('myshow')).toEqual({ mode: 1, events: [
      { t: 0, k: 0, type: 'wcb_unicast', target: '2', cmd: ';A,NEXT' },
      { t: 1500, k: 0, type: 'wcb_unicast', target: '2', cmd: ';A,STOP' },
    ].map(normEvent) });
    await page.locator('#tl-close-btn').click();
    await expect(page.locator('#tl-modal')).not.toHaveClass(/open/);
    await expect(rowButton(page, 'myshow', '📈')).toBeVisible({ timeout: 20_000 });
    T.expectNoPageErrors(page);
  });

  test('nctool.timeline_preview_offline with no board, Preview plays the cursor through the clip in the browser, lighting each action as it passes, asks nothing and sends nothing', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, []);
    await T.openTool(page);
    await theme(page);
    await page.evaluate(() => {
      openNewTimeline('offline');
      _tlAddResolvedAction({ type: 'mp3', fn: 1, track: 2 }, 500);
      _tlClip.selected = null;
      _tlSetLength(1);
    });
    await page.locator('#tl-play-btn').click();
    await expect(page.locator('#tl-play-btn')).toHaveText('■ Stop');
    await page.clock.runFor(580);
    await expect(page.locator('#tl-svg g.tl-ev.flash')).toHaveCount(1);
    await shot(page, '06-preview-offline');
    await page.clock.runFor(1000);
    await expect(page.locator('#tl-play-btn')).toHaveText('▶ Preview');
    await expect(page.locator('#tl-svg g.tl-ev.flash')).toHaveCount(0);
    expect(await page.evaluate(() => [_tlClip.cursorMs, _tlLive])).toEqual([1000, false]);
    expect(dialogs).toEqual([]);
    expect(emu.rx).toEqual([]);
    T.expectNoPageErrors(page);
  });
});
