// The timeline editor's tools (docs/hil_plan/NAVICORE.md §2, nct.timeline.editor, L1). nctool.timeline_editor_save
// covers the open and the save; here the editing: a keyframe selected by its dot and removed with Delete or a
// right-click, Undo and Ctrl+Z over the history (_tlPushHistory, config_tool/index.html ~8625), Trim start, the length
// grown (servos hold) and shortened behind a confirm (_tlSetLength ~8454), a Maestro track added, an action added,
// edited and deleted through the popover with its free-time rule (_tlOpenActionEditor ~8891), an easing pair picked
// with Shift and applied (_tlApplyEasing ~8579), Smooth on the focused channel (_tlSmoothTrack ~8669), zoom; and the
// previews: Live frees the Maestro channels and streams ?MAE as the cursor moves (_tlSendLiveAt ~8305), Preview plays
// the clip to its end, and the Maestro script export (_tlExportMaestroScript ~8750). The emulator's clip store serves
// the clips as clips.spec.js does.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const { normEvent } = require('../../lib/navicore/clips');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));

// "wave" as clips.spec.js builds it: 24 Maestro keyframes on M1 ch 0/1 (t = 120 i, pos = 6000 + 25 i, even i on ch 0),
// an HCR track and three actions; "late" starts 400 ms in, so Trim has something to take.
function waveEvents() {
  const ev = [];
  for (let i = 0; i < 24; i++) ev.push({ t: i * 120, k: 1, slot: 1, ch: i % 2, pos: 6000 + i * 25 });
  ev.push({ t: 300, k: 2, chan: 0, vol: 40 }, { t: 1500, k: 2, chan: 0, vol: 70 }, { t: 2700, k: 2, chan: 0, vol: 20 });
  ev.push({ t: 600, k: 0, type: 'wcb_unicast', target: '2', cmd: ';S1wave' }, { t: 1200, k: 0, type: 'mp3', fn: 1, track: 5 },
          { t: 2400, k: 0, type: 'play', cmd: 'beep', fn: 0 });
  return ev.sort((a, b) => a.t - b.t).map(normEvent);
}
const LATE = [{ t: 400, k: 1, slot: 2, ch: 0, pos: 5000 }, { t: 600, k: 0, type: 'stop' }, { t: 900, k: 1, slot: 2, ch: 0, pos: 7000 }].map(normEvent);
const CLIPS = () => ({ wave: { mode: 2, events: waveEvents() }, late: { mode: 1, events: LATE } });

const rowButton = (page, name, label) =>
  page.locator('#clips-list > div').filter({ has: page.getByText(name, { exact: true }) }).locator('button', { hasText: label });
async function openClip(page, name, status) {
  await page.locator('#btn-clips').click();
  await expect(page.locator('#clips-modal')).toHaveClass(/open/);
  await rowButton(page, name, '📈').click();
  await expect(page.locator('#tl-modal')).toHaveClass(/open/);
  await expect(page.locator('#tl-status')).toHaveText(status, { timeout: 20_000 });
}
const status = (page) => page.locator('#tl-status');
const model = (page) => page.evaluate(() => ({
  maestro: Object.fromEntries(Object.entries(_tlClip.maestro).map(([k, a]) => [k, a.map((f) => [f.t, f.pos])])),
  hcr: Object.fromEntries(Object.entries(_tlClip.hcr).map(([k, a]) => [k, a.map((f) => [f.t, f.vol])])),
  actions: _tlClip.actions.map((a) => [a.t, JSON.parse(JSON.stringify(a.action))]), dur: _tlClip.durationMs,
}));
const actionTypes = async (page) => (await model(page)).actions.map(([t, a]) => [t, a.type]);
// A keyframe's hit circle, found by the x its time maps to (no two keyframes of the bench clips share a time).
async function dot(page, key, idx) {
  const cx = await page.evaluate(([k, i]) => String(_tlTimeToX(_tlClip.maestro[k][i].t)), [key, idx]);
  return page.locator(`#tl-svg circle[fill="transparent"][cx="${cx}"]`);
}
// A track's name in the gutter: its <text> carries a <title> ('<label> — click to focus…'), and the label may end
// in ' · <servo name>'. Matching the title keeps 'M1 Ch1' from matching 'M1 Ch10' (or its own title's repeat).
const trackLabel = (page, name) => page.locator('#tl-svg text', { hasText: new RegExp(`${name}( · [^—]*)? — click to focus this channel`) });
async function setLength(page, v) {
  await page.locator('#tl-length').fill(v);
  await page.locator('#tl-length').dispatchEvent('change');
}

test.describe('the timeline editor', () => {
  test.use({ emuOptions: { config: BENCH, clips: CLIPS() } });

  test('nctool.timeline_edit_tools a dot clicked is selected and its track focused, Delete removes it and Undo restores it (the history empties); a right-click deletes a dot and Ctrl+Z undoes; Trim refuses a clip starting at 0; two adjacent dots Shift-clicked make an easing pair and Apply inserts the curve\'s steps between them, a non-adjacent pair is refused; Smooth needs a focused channel (the track name focuses it) and keeps the ends; zoom scales the axis; closing with edits asks first', async ({ page, emu }) => {
    const answers = [];
    const dialogs = T.answerDialogs(page, answers);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClip(page, 'wave', '30 events · 2.8 s');
    const before = await model(page);
    expect(before.maestro['1-0']).toHaveLength(12);
    expect(before.maestro['1-0'][3]).toEqual([720, 6150]);
    await expect(page.locator('#tl-undo')).toBeDisabled();
    await expect(page.locator('#tl-delete-btn')).toBeHidden();
    await expect(page.locator('#tl-length')).toHaveValue('2.8');
    // Select, Delete, Undo.
    await (await dot(page, '1-0', 3)).click();
    expect(await page.evaluate(() => [_tlClip.selected.kind, _tlClip.selected.key, _tlClip.activeTrack])).toEqual(['maestro', '1-0', 'maestro:1-0']);
    await expect(page.locator('#tl-delete-btn')).toBeVisible();
    await page.keyboard.press('Delete');
    expect((await model(page)).maestro['1-0']).toEqual(before.maestro['1-0'].filter((_, i) => i !== 3));
    await expect(page.locator('#tl-undo')).toBeEnabled();
    await expect(page.locator('#tl-delete-btn')).toBeHidden();
    await page.locator('#tl-undo').click();
    expect((await model(page)).maestro['1-0']).toEqual(before.maestro['1-0']);
    await expect(page.locator('#tl-undo')).toBeDisabled();
    // A right-click deletes; Ctrl+Z undoes.
    await (await dot(page, '1-1', 2)).click({ button: 'right' });
    expect((await model(page)).maestro['1-1']).toEqual(before.maestro['1-1'].filter((_, i) => i !== 2));
    await page.keyboard.press('ControlOrMeta+z');
    expect((await model(page)).maestro['1-1']).toEqual(before.maestro['1-1']);
    // Nothing to trim here.
    await page.locator('#tl-trim-start').click();
    await expect(status(page)).toHaveText('No empty space at the start to trim.');
    // Easing between two adjacent dots: ease-in-out in 4 steps inserts three keyframes.
    await (await dot(page, '1-0', 2)).click({ modifiers: ['Shift'] });
    await expect(page.locator('#tl-easing-toolbar')).toBeHidden();
    await (await dot(page, '1-0', 3)).click({ modifiers: ['Shift'] });
    await expect(page.locator('#tl-easing-toolbar')).toBeVisible();
    await page.locator('#tl-ease-fn').selectOption('easeInOut');
    await page.locator('#tl-ease-steps').fill('4');
    await page.locator('#tl-easing-toolbar button', { hasText: 'Apply easing' }).click();
    const eased = (await model(page)).maestro['1-0'];
    expect(eased).toHaveLength(15);
    expect(eased.slice(2, 7)).toEqual([[480, 6100], [540, 6106], [600, 6125], [660, 6144], [720, 6150]]);
    await expect(page.locator('#tl-easing-toolbar')).toBeHidden();
    // Not adjacent: refused, nothing selected as a pair.
    await (await dot(page, '1-1', 0)).click({ modifiers: ['Shift'] });
    await (await dot(page, '1-1', 5)).click({ modifiers: ['Shift'] });
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringContaining('Pick two ADJACENT keyframes'));
    expect(await page.evaluate(() => _tlClip.selectedPair)).toBeNull();
    // Smooth: no focused channel says how to focus one; the track name focuses it; the ends and the times stay.
    await page.evaluate(() => { _tlClip.activeTrack = null; _tlClip.selected = null; _tlRender(); });
    await page.locator('#tl-smooth').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringContaining('Smooth works on one channel'));
    await trackLabel(page, 'M1 Ch1').click();
    expect(await page.evaluate(() => _tlClip.activeTrack)).toBe('maestro:1-1');
    await page.locator('#tl-smooth').click();
    const sm = (await model(page)).maestro['1-1'];
    expect(sm.map(([t]) => t)).toEqual(before.maestro['1-1'].map(([t]) => t));
    expect([sm[0], sm[11]]).toEqual([before.maestro['1-1'][0], before.maestro['1-1'][11]]);
    expect(sm).not.toEqual(before.maestro['1-1']);
    expect(Math.min(...sm.map(([, p]) => p))).toBeGreaterThanOrEqual(6025);
    expect(Math.max(...sm.map(([, p]) => p))).toBeLessThanOrEqual(6575);
    await page.locator('#tl-undo').click();
    expect((await model(page)).maestro['1-1']).toEqual(before.maestro['1-1']);
    // Zoom.
    const px0 = await page.evaluate(() => _tlClip.pxPerMs);
    await page.locator('#tl-zoom-in').click();
    expect(await page.evaluate(() => _tlClip.pxPerMs)).toBeCloseTo(px0 * 1.4, 5);
    await page.locator('#tl-zoom-fit').click();
    expect(await page.evaluate(() => _tlClip.pxPerMs)).not.toBeCloseTo(px0 * 1.4, 5);
    // Closing with edits asks; declined, the editor stays.
    answers.push(false);
    await page.locator('#tl-close-btn').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual('Discard unsaved timeline edits?');
    await expect(page.locator('#tl-modal')).toHaveClass(/open/);
    answers.push(true);
    await page.locator('#tl-close-btn').click();
    await expect(page.locator('#tl-modal')).not.toHaveClass(/open/);
    T.expectNoPageErrors(page);
  });

  test('nctool.timeline_length_tracks_actions Trim start moves everything to 0 and shortens the clip; a longer length holds each servo\'s last pose to the new end, a shorter one asks and deletes what lies past it (declined, the field goes back); + Maestro asks a slot and a channel and adds the empty track; + Action opens the popover at the cursor, needs a command, lands an action on a free time (5 ms past another\'s), edits it pre-filled and deletes it, and offers no Delay (a clip never applies one) while keeping one an action carries; with no servo keyframes the script export says so', async ({ page, emu }) => {
    const answers = [];
    const dialogs = T.answerDialogs(page, answers);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClip(page, 'late', '3 events · 900 ms');
    expect((await model(page)).maestro).toEqual({ '2-0': [[400, 5000], [900, 7000]] });
    expect(await actionTypes(page)).toEqual([[600, 'stop']]);
    await page.locator('#tl-trim-start').click();
    await expect(status(page)).toHaveText('Trimmed 400 ms of leading empty space.');
    expect(await model(page)).toMatchObject({ maestro: { '2-0': [[0, 5000], [500, 7000]] }, dur: 500 });
    expect(await actionTypes(page)).toEqual([[200, 'stop']]);
    await expect(page.locator('#tl-length')).toHaveValue('0.5');
    // Longer: the last pose is held to the new end.
    await setLength(page, '1.0');
    await expect(status(page)).toHaveText('Extended to 1.0 s (servos hold — drag the end points to animate).');
    expect((await model(page)).maestro['2-0']).toEqual([[0, 5000], [500, 7000], [1000, 7000]]);
    // Shorter: asked; declined keeps everything and puts the field back; accepted deletes past the end.
    answers.push(false);
    await setLength(page, '0.1');
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual('Shorten the clip to 100 ms? This deletes 3 keyframe/action(s) after that point.');
    await expect(page.locator('#tl-length')).toHaveValue('1.0');
    expect(await model(page)).toMatchObject({ maestro: { '2-0': [[0, 5000], [500, 7000], [1000, 7000]] }, dur: 1000 });
    answers.push(true);
    await setLength(page, '0.3');
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual('Shorten the clip to 300 ms? This deletes 2 keyframe/action(s) after that point.');
    await expect(status(page)).toHaveText('Shortened to 300 ms.');
    expect(await model(page)).toMatchObject({ maestro: { '2-0': [[0, 5000]] }, dur: 300 });
    expect(await actionTypes(page)).toEqual([[200, 'stop']]);
    // + Maestro.
    answers.push('3', '5');
    await page.locator('#tl-add-maestro').click();
    await expect.poll(() => dialogs.slice(-2).map((d) => d.message)).toEqual(['Maestro slot (1-8):', 'Maestro channel (0-31):']);
    expect(Object.keys((await model(page)).maestro).sort()).toEqual(['2-0', '3-5']);
    await expect(trackLabel(page, 'M3 Ch5')).toHaveCount(1);
    // + Action.
    await page.locator('#tl-add-action').click();
    await expect(page.locator('#tl-action-modal')).toHaveClass(/open/);
    await expect(page.locator('#tl-action-title')).toHaveText('New action');
    await expect(page.locator('#tl-action-time')).toHaveValue('0');
    await expect(page.locator('#tl-action-delete')).toBeHidden();
    await page.locator('#tl-action-type').selectOption('wcb_unicast');
    await page.locator('#tl-action-apply').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringContaining('Fill in the required fields'));
    await expect(page.locator('#tl-action-modal')).toHaveClass(/open/);
    await page.locator('#tl-action-fields-cmd').fill(';S2HILTL');
    await page.locator('#tl-action-time').fill('200');   // the stop's time: the new action lands 5 ms past it
    // No Delay field: a clip replays each action through rcExecuteActionNow (NaviCore.ino recCbDispatch), which never
    // applies one, so an action fires at its time.
    await expect(page.locator('#tl-action-delay')).toHaveCount(0);
    await expect(page.locator('#tl-action-delay-note')).toBeHidden();
    await page.locator('#tl-action-note').fill('tl note');
    await page.locator('#tl-action-apply').click();
    await expect(page.locator('#tl-action-modal')).not.toHaveClass(/open/);
    const acts = (await model(page)).actions;
    expect(acts.map(([t, a]) => [t, a.type])).toEqual([[200, 'stop'], [205, 'wcb_unicast']]);
    expect(acts[1][1]).toEqual({ type: 'wcb_unicast', target: '1', cmd: ';S2HILTL', note: 'tl note' });
    // A delay a recorded action carries is kept as it is, and the popover says a clip does not apply it.
    await page.evaluate(() => { _tlClip.actions[1].action.delay = 20; });
    // Edit it: pre-filled; its Delete removes it.
    await page.evaluate(() => _tlOpenActionEditor(_tlClip.actions[1], 0));
    await expect(page.locator('#tl-action-title')).toHaveText('Edit action');
    await expect(page.locator('#tl-action-time')).toHaveValue('205');
    await expect(page.locator('#tl-action-fields-cmd')).toHaveValue(';S2HILTL');
    await expect(page.locator('#tl-action-delay-note')).toHaveText('This action carries a 20 ms delay, which a clip does not apply: it fires at its time. Move it to fire later.');
    await expect(page.locator('#tl-action-note')).toHaveValue('tl note');
    await expect(page.locator('#tl-action-delete')).toBeVisible();
    await page.locator('#tl-action-fields-cmd').fill(';S2HILTL2');
    await page.locator('#tl-action-apply').click();
    expect((await model(page)).actions[1][1]).toMatchObject({ cmd: ';S2HILTL2', delay: 20, note: 'tl note' });
    await page.evaluate(() => _tlOpenActionEditor(_tlClip.actions[1], 0));
    await page.locator('#tl-action-delete').click();
    await expect(page.locator('#tl-action-modal')).not.toHaveClass(/open/);
    expect(await actionTypes(page)).toEqual([[200, 'stop']]);
    // No servo keyframes left (the one track deleted behind its confirm): nothing to export.
    answers.push(true);
    await page.evaluate(() => _tlDeleteTrack(_tlTrackList()[0]));
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringMatching(/^Delete the entire "M2 Ch0.*" track and all its keyframes\?$/));
    expect(Object.keys((await model(page)).maestro)).toEqual(['3-5']);
    await page.locator('#tl-export-script').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual('This clip has no servo keyframes to export.');
    answers.push(true);
    await page.locator('#tl-close-btn').click();
    await expect(page.locator('#tl-modal')).not.toHaveClass(/open/);
    T.expectNoPageErrors(page);
  });

  test('nctool.timeline_preview_export Live frees each Maestro channel the clip drives and sends the pose at the cursor, Preview plays the cursor to the end streaming ?MAE for every change and lands on the last pose, Live off stops the stream; the script export downloads a Maestro script for the clip\'s one slot with the keyframes reduced to frames', async ({ page, emu }) => {
    const downloads = T.captureDownloads(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClip(page, 'wave', '30 events · 2.8 s');
    expect(emu.lines(/^\?MAE/)).toEqual([]);
    await expect(page.locator('#tl-live-btn')).toHaveText('⏻ Live');
    await page.locator('#tl-live-btn').click();
    await expect(page.locator('#tl-live-btn')).toHaveText('⏻ Live ON');
    await expect.poll(() => emu.lines(/^\?MAE/)).toEqual(['?MAE,FREE,1,0', '?MAE,FREE,1,1', '?MAE,1,0,6000', '?MAE,1,1,6025']);
    // Preview: the cursor runs to the end; the stream follows it and lands on the last pose.
    await page.locator('#tl-play-btn').click();
    await expect(page.locator('#tl-play-btn')).toHaveText('■ Stop');
    await page.clock.runFor(3000);
    await expect(page.locator('#tl-play-btn')).toHaveText('▶ Preview');
    expect(await page.evaluate(() => [_tlClip.cursorMs, _tlLivePlaying])).toEqual([2760, false]);
    const ch0 = emu.lines(/^\?MAE,1,0,/).map((l) => +l.split(',')[3]);
    expect(ch0.length).toBeGreaterThan(5);
    expect(ch0[ch0.length - 1]).toBe(6550);
    expect(ch0.every((v, i) => i === 0 || v >= ch0[i - 1]), 'monotonic along the ramp').toBe(true);
    expect(emu.lines(/^\?MAE,1,1,/).pop()).toBe('?MAE,1,1,6575');
    // Live off: nothing more streams.
    await page.locator('#tl-live-btn').click();
    await expect(page.locator('#tl-live-btn')).toHaveText('⏻ Live');
    const n = emu.lines(/^\?MAE/).length;
    await page.evaluate(() => { _tlClip.cursorMs = 1000; _tlRender(); _tlLiveTick(); });
    await page.clock.runFor(200);
    expect(emu.lines(/^\?MAE/)).toHaveLength(n);
    // Export: the linear ramps reduce to two frames per channel.
    await page.locator('#tl-export-script').click();
    await expect(status(page)).toHaveText(/^Exported Maestro 1 script: \d+ frames, ~\d+ B compiled \(tolerance \d+ µs\)\.$/);
    const frames = +(await status(page).textContent()).match(/(\d+) frames/)[1];
    await expect.poll(() => downloads.length).toBe(1);
    expect(downloads[0].name).toBe('clip_wave_maestro_script.txt');
    const lines = (await downloads[0].ready).split('\n');
    expect(lines[0]).toBe('# NaviCore clip export — "wave" → Maestro 1');
    expect(lines).toContain('sub clip_wave');
    expect(lines.filter((l) => /^  \d+( \d+)+ frame_[0-9_]+$/.test(l) || /^  \d+ delay$/.test(l))).toHaveLength(frames);
    expect(lines.filter((l) => /^sub frame_/.test(l)).length).toBeGreaterThan(0);
    expect(lines[lines.length - 2]).toBe('  return');
    await page.locator('#tl-close-btn').click();
    await expect(page.locator('#tl-modal')).not.toHaveClass(/open/);
    T.expectNoPageErrors(page);
  });
});
