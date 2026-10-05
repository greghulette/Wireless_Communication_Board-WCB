// Record/replay clips in the config tool (docs/hil_plan/NAVICORE.md §2, nct.clips.* and nct.timeline.editor, L1): the
// Clips panel's list forms, record / rename / delete with the trigger actions that name a clip, the verified ranged
// download, the backup bundle, the restore, and the timeline editor's load and save. No board: the emulator holds a
// clip store and answers ?REC the way NaviCore.ino:3440-3595 and navicore_record.h do (lib/navicore/clips.js), lost
// lines, lost ACKs and a truncating buffer included. Nothing here reaches a file system except the downloads the tool
// offers, which Playwright captures.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const { normEvent } = require('../../lib/navicore/clips');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
// A button that plays "wave" and another that records over it: what a rename re-points and a delete offers to remove.
const CFG = JSON.parse(JSON.stringify(BENCH));
CFG.mappings['119'] = { exclusive: false, t1: [{ type: 'play', cmd: 'wave', fn: 0 }] };
CFG.mappings['120'] = { exclusive: false, t2: [{ type: 'record', cmd: 'wave' }] };

// 30 events over 2.76 s: two Maestro tracks, an HCR-volume track and three actions, mode 2.
function waveEvents() {
  const ev = [];
  for (let i = 0; i < 24; i++) ev.push({ t: i * 120, k: 1, slot: 1, ch: i % 2, pos: 6000 + i * 25 });
  ev.push({ t: 300, k: 2, chan: 0, vol: 40 }, { t: 1500, k: 2, chan: 0, vol: 70 }, { t: 2700, k: 2, chan: 0, vol: 20 });
  ev.push({ t: 600, k: 0, type: 'wcb_unicast', target: '2', cmd: ';S1wave' }, { t: 1200, k: 0, type: 'mp3', fn: 1, track: 5 },
          { t: 2400, k: 0, type: 'play', cmd: 'beep', fn: 0 });
  return ev.sort((a, b) => a.t - b.t).map(normEvent);
}
const BEEP = [{ t: 0, k: 1, slot: 2, ch: 0, pos: 5000 }, { t: 100, k: 0, type: 'stop' }, { t: 200, k: 1, slot: 2, ch: 0, pos: 7000 }].map(normEvent);
const CLIPS = () => ({ wave: { mode: 2, events: waveEvents() }, beep: { mode: 1, events: BEEP } });

async function openClips(page) {
  await page.locator('#btn-clips').click();
  await expect(page.locator('#clips-modal')).toHaveClass(/open/);
}
const rowNames = (page) => page.locator('#clips-list > div > div:first-child > div:first-child').allTextContents();
const rowButton = (page, name, label) =>
  page.locator('#clips-list > div').filter({ has: page.getByText(name, { exact: true }) }).locator('button', { hasText: label });

test.describe('the Clips panel over Direct USB', () => {
  test.use({ emuOptions: { config: CFG, clips: CLIPS() } });

  test('nctool.clips_record_rename_delete Record captures and saves under the typed name; a rename re-points the play/record actions that name the clip, a clash or a board refusal changes nothing; a delete offers to remove the actions that used it', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, ['wave2', null, 'beep', null, 'bop', null, true, true, null]);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep']);
    await expect(page.locator('#clips-storage')).toBeVisible();
    expect(await page.evaluate(() => [...document.querySelectorAll('#clip-name-options option')].map((o) => o.value))).toEqual(['wave', 'beep']);

    // Record: START, (no [CLIPUL:REC] marker on this firmware: the tool waits 1.5 s and proceeds), capture, Stop & Save.
    await page.locator('#clip-record-name').fill('take 1!');
    await page.locator('#clip-record-btn').click();
    await expect.poll(() => emu.rec.state).toBe('recording');
    await page.clock.fastForward(1600);
    await expect(page.locator('#clip-record-btn')).toHaveText('■ Stop & Save');
    emu.recCapture([{ t: 0, k: 1, slot: 1, ch: 0, pos: 6000 }, { t: 40, k: 1, slot: 1, ch: 0, pos: 6400 }]);
    await page.locator('#clip-record-btn').click();
    await expect(page.locator('#clip-record-btn')).toHaveText('● Record');
    expect(emu.lines(/^\?REC,(STOP|SAVE)/)).toEqual(['?REC,STOP', '?REC,SAVE,take1']);   // the name as the board stores it
    await page.clock.fastForward(600);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep', 'take1']);
    expect(emu.clips.get('take1')).toEqual({ mode: emu.mode_, events: [{ t: 0, k: 1, slot: 1, ch: 0, pos: 6000 }, { t: 40, k: 1, slot: 1, ch: 0, pos: 6400 }] });

    // Rename: the board confirms with [CLIPUL:RENAME,OK], and both actions follow the clip.
    await rowButton(page, 'wave', '✎').click();
    await expect.poll(() => dialogs.length).toBe(2);
    expect(emu.lines(/^\?REC,RENAME,/)).toEqual(['?REC,RENAME,wave,wave2']);
    expect(dialogs[1].message).toBe('Renamed. 2 button/switch action(s) now reference "wave2" — click Save to store the updated config.');
    expect(await page.evaluate(() => [config.mappings['119'].t1[0].cmd, config.mappings['120'].t2[0].cmd])).toEqual(['wave2', 'wave2']);
    await page.clock.fastForward(500);
    await expect.poll(() => rowNames(page)).toEqual(['beep', 'take1', 'wave2']);   // the renamed file lists last
    // Onto a name that exists: refused before anything is sent.
    await rowButton(page, 'wave2', '✎').click();
    await expect.poll(() => dialogs.length).toBe(4);
    expect(dialogs[3].message).toBe('A clip named "beep" already exists — choose a different name.');
    expect(emu.lines(/^\?REC,RENAME,/)).toHaveLength(1);
    // The board refuses (its own clip list changed under the tool): nothing is re-pointed.
    emu.cli = (line, out) => {
      if (!/^\?REC,RENAME,beep,/.test(line)) return false;
      out('[REC] rename failed (exists / not found)'); out('[CLIPUL:RENAME,ERR]');
      return true;
    };
    await rowButton(page, 'beep', '✎').click();
    await expect.poll(() => dialogs.length).toBe(6);
    expect(dialogs[5].message).toBe('The board refused the rename (a clip named "bop" may already exist there). Nothing was changed.');
    expect(emu.clips.has('beep')).toBe(true);
    emu.cli = null;

    // Delete: confirm, RM, then the two actions that played / recorded it go too (on a second yes).
    await rowButton(page, 'wave2', '🗑').click();
    await expect.poll(() => dialogs.length).toBe(9);
    expect(dialogs.slice(6).map((d) => d.message)).toEqual([
      'Delete clip "wave2"? This cannot be undone.',
      'This clip was used by 2 button/switch action(s). Remove those trigger actions too? (You\'ll then need to Save the config.)',
      'Removed 2 trigger action(s). Click Save to store the updated config.',
    ]);
    expect(emu.lines(/^\?REC,RM,/)).toEqual(['?REC,RM,wave2']);
    expect(emu.clips.has('wave2')).toBe(false);
    expect(await page.evaluate(() => [config.mappings['119'].t1, config.mappings['120'].t2])).toEqual([[], []]);
    expect(await page.evaluate(() => _configUnsaved())).toBe(true);        // the action edits wait for a Save
    await page.clock.fastForward(500);
    await expect.poll(() => rowNames(page)).toEqual(['beep', 'take1']);
    T.expectNoPageErrors(page);
  });

  test('nctool.clip_record_refused (should) a Record the board refuses because it is busy replaying does not arm Stop & Save, whose SAVE would store the replayed clip under the typed name', async ({ page, emu }) => {
    // A minute-long clip, so the replay is certainly still running when START arrives.
    emu.clips.set('minute', { mode: 1, events: [{ t: 0, k: 1, slot: 1, ch: 0, pos: 6000 }, { t: 60_000, k: 1, slot: 1, ch: 0, pos: 7000 }] });
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep', 'minute']);
    await rowButton(page, 'minute', '▶ Play').click();
    await expect.poll(() => emu.rec.state).toBe('replaying');
    await page.locator('#clip-record-name').fill('newtake');
    await page.locator('#clip-record-btn').click();
    await expect.poll(() => emu.lines(/^\?REC,START$/).length).toBe(1);
    expect(emu.rec.state, 'START arrived during the replay').toBe('replaying');
    await page.clock.fastForward(1600);
    await expect(page.locator('#clip-record-btn'), 'the Record button after a refused START').toHaveText('● Record');
  });

  test('nctool.clip_download_verified a clip downloads to a file only when every event arrived: a lost line is re-requested by index, while a truncated buffer, a clip that changed mid-download and an event that never arrives each save nothing', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page);
    const files = T.captureDownloads(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep']);
    const wave = emu.clips.get('wave');
    const download = async () => {
      const n = files.length, d = dialogs.length, r = emu.lines(/^\?REC,EDITLOAD,/).length;
      await rowButton(page, 'wave', '⤓').click();
      await expect.poll(() => files.length - n + dialogs.length - d, { timeout: 20_000 }).toBe(1);
      await expect(rowButton(page, 'wave', '⤓')).toBeEnabled();
      const file = files[n] ? { name: files[n].name, text: await files[n].ready } : null;
      return { file, alert: dialogs[d] || null, ranges: emu.lines(/^\?REC,EDITLOAD,/).slice(r) };
    };

    // Clean, over USB: a header probe, then one 400-event slice with keyframes batched.
    let got = await download();
    expect(got.ranges).toEqual(['?REC,EDITLOAD,wave,0,0,B', '?REC,EDITLOAD,wave,0,30,B']);
    expect(got.file.name).toBe('navicore-clip-wave.json');
    let obj = JSON.parse(got.file.text);
    expect(obj).toMatchObject({ navicore: 'clip/v1', clip: { name: 'wave', durationMs: 2760, mode: 2 } });
    expect(obj.clip.events).toEqual(wave.events);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Saved "wave" — 30 events'));

    // A lost batch line: only the indices it carried are asked for again.
    emu.clipFault.dropLine = new Set([5]);
    got = await download();
    const miss = emu.recLog.filter((r) => r.op === 'dropped').pop().idxs;
    expect(got.ranges).toEqual(['?REC,EDITLOAD,wave,0,0,B', '?REC,EDITLOAD,wave,0,30,B', `?REC,EDITLOAD,wave,${miss[0]},${miss.length},B`]);
    expect(JSON.parse(got.file.text).clip.events).toEqual(wave.events);

    // The board's buffer holds fewer events than the file (loadClip truncates silently): refused.
    emu.clipFault.cap = 10; emu.rec.loadedName = '';
    got = await download();
    expect(got.file).toBeNull();
    expect(got.alert.message).toBe('Could not download "wave" completely, so nothing was saved.\n\nclip is truncated on the board (file says 30 events, buffer holds 10)');
    delete emu.clipFault.cap; emu.rec.loadedName = '';

    // The clip is re-recorded between the probe and the slice: its fingerprint changes, and nothing is spliced.
    emu.cli = (line, out, e) => {
      if (!/^\?REC,EDITLOAD,wave,0,0,B$/.test(line)) return false;
      e._recCli(line, out, false);
      e.clipReplace('wave', waveEvents().map((ev) => (ev.k === 1 ? { ...ev, pos: ev.pos + 1 } : ev)));
      return true;
    };
    got = await download();
    expect(got.file).toBeNull();
    expect(got.alert.message).toBe('Could not download "wave" completely, so nothing was saved.\n\nthe clip changed on the board mid-download — start again');
    emu.cli = null;
    emu.clipReplace('wave', waveEvents(), 2);

    // One event never arrives however often it is asked for: the rounds run out and nothing is written.
    emu.clipFault.dropAlways = new Set([25]);                       // the play action: a line of its own
    got = await download();
    expect(got.file).toBeNull();
    expect(got.alert.message).toBe('Could not download "wave" completely, so nothing was saved.\n\n1 of 30 events would not transfer');
    expect(got.ranges.length).toBe(1 + 15);                            // the probe, then ceil(30/400) * 3 + 12 rounds
    T.expectNoPageErrors(page);
  });

  test('nctool.clip_backup_bundle "All + config" warns with the size and a time estimate first (declined, nothing moves), then writes the config and every clip that downloaded completely, naming the one that did not', async ({ page, emu }) => {
    emu.clips.set('huge', { mode: 3, events: waveEvents().map((ev) => ({ ...ev })) });
    emu.clipFault.dropAlways = new Set();
    const dialogs = T.answerDialogs(page, [false, true, null]);
    const files = T.captureDownloads(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep', 'huge']);
    const all = page.locator('#clips-modal button', { hasText: '⤓ All + config' });
    await all.click();
    await expect.poll(() => dialogs.length).toBe(1);
    // [CLIPITEM] bytes and n: 2 x 30 events and 1 x 3, at the emulator's 16 + 140 B an event.
    expect(dialogs[0].message).toBe(`Download all 3 clip(s)?\n\n${((2 * (16 + 140 * 30) + 16 + 140 * 3) / 1024).toFixed(1)} KB · 63 events\n` +
                                    'Estimated 1 seconds over Direct USB.');
    expect(emu.lines(/^\?REC,EDITLOAD,/)).toEqual([]);
    expect(files).toEqual([]);

    // "huge" loses the same batch line every time it is asked: that clip is left out, and said so.
    emu.cli = (line, out, e) => {
      if (/^\?REC,EDITLOAD,huge,/.test(line)) e.clipFault.dropAlways = new Set([7]);   // its first action
      else if (/^\?REC,EDITLOAD,/.test(line)) e.clipFault.dropAlways = new Set();
      return false;
    };
    await all.click();
    await expect.poll(() => files.length, { timeout: 30_000 }).toBe(1);
    await expect.poll(() => dialogs.length).toBe(3);
    expect(files[0].name).toMatch(/^navicore-backup-\d{4}-\d{2}-\d{2}\.json$/);
    const obj = JSON.parse(await files[0].ready);
    expect(Object.keys(obj)).toEqual(['navicore', 'savedAt', 'note', 'config', 'clips']);
    expect(obj.navicore).toBe('config-backup/v1');
    expect(obj.config).toEqual(await page.evaluate(() => JSON.parse(JSON.stringify(config))));
    expect(obj.clips.map((c) => c.name)).toEqual(['wave', 'beep']);
    expect(obj.clips[0]).toEqual({ name: 'wave', durationMs: 2760, mode: 2, events: emu.clips.get('wave').events });
    expect(obj.clips[1]).toEqual({ name: 'beep', durationMs: 200, mode: 1, events: BEEP });
    expect(dialogs[2].message).toBe('Saved config + 2 clip(s).\n\nThese could NOT be downloaded completely and were LEFT OUT:\n' +
                                    '  • huge — 1 of 30 events would not transfer\n\nTry again over Direct USB for those.');
    T.expectNoPageErrors(page);
  });

  test('nctool.clip_restore a restore asks per collision (skip, a new name that is re-checked, overwrite), writes each event at its index so a lost ACK\'s retry adds no duplicate, reads the list back, and reports what it did', async ({ page, emu }) => {
    emu.clips.set('fresh-old', { mode: 1, events: BEEP.map((e) => ({ ...e })) });
    const restoreFile = {
      navicore: 'config-backup/v1', savedAt: 1, note: '', config: {},
      clips: [
        { name: 'wave', durationMs: 2760, mode: 2, events: waveEvents().slice(0, 5) },     // collides: skipped
        { name: 'beep', durationMs: 300, mode: 1, events: BEEP.map((e) => ({ ...e, t: e.t + 100 })) },   // collides: renamed twice
        { name: 'fresh', durationMs: 2760, mode: 2, events: waveEvents() },                // new
        { name: 'fresh-old', durationMs: 300, mode: 1, events: [BEEP[0]] },                  // collides: overwritten
      ],
    };
    const dialogs = T.answerDialogs(page, ['skip', 'wave', 'beep2', 'overwrite', null]);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep', 'fresh-old']);
    emu.clipFault.lostAck = new Set([2]);
    const chooser = page.waitForEvent('filechooser');
    await page.locator('#clips-modal button', { hasText: '⤒ Restore' }).click();
    await (await chooser).setFiles({ name: 'backup.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(restoreFile)) });
    // The first clip written ("beep2") loses the ACK of event 2: the tool's 4 s wait runs out and it resends.
    await expect.poll(() => emu.recLog.some((r) => r.op === 'lostAck'), { timeout: 20_000 }).toBe(true);
    await page.clock.fastForward(4100);
    await expect.poll(() => dialogs.length, { timeout: 30_000 }).toBe(5);
    expect(dialogs.slice(0, 4).map((d) => [d.type, d.message.split('\n')[0]])).toEqual([
      ['prompt', '"wave" already exists on the droid.'],
      ['prompt', '"beep" already exists on the droid.'],
      ['prompt', '"wave" already exists on the droid.'],        // the typed new name is itself taken: asked again
      ['prompt', '"fresh-old" already exists on the droid.'],
    ]);
    expect(dialogs[4].message).toBe('Restored 3 clip(s).\nSkipped (already on the droid): wave');
    expect(emu.clips.get('wave').events).toEqual(waveEvents());                          // untouched
    expect(emu.clips.get('beep').events).toEqual(BEEP);                                  // untouched
    expect(emu.clips.get('beep2').events).toEqual(restoreFile.clips[1].events);          // no duplicate from the retry
    expect(emu.clips.get('fresh').events).toEqual(waveEvents());
    expect(emu.clips.get('fresh-old').events).toEqual([BEEP[0]]);
    expect(emu.lines(/^\?REC,EDITEV,/)).toHaveLength(3 + 1 + 30 + 1);                   // every event once, and the one resend
    expect(emu.lines(/^\?REC,EDITEND,/)).toEqual(['?REC,EDITEND,beep2', '?REC,EDITEND,fresh', '?REC,EDITEND,fresh-old']);
    expect(emu.lines(/^\?REC,EDITCANCEL/)).toEqual([]);
    expect(emu.rec.state).toBe('idle');
    T.expectNoPageErrors(page);
  });

  test('nctool.clip_restore_mode (should) a restored clip keeps the mode it was recorded in, not whichever clip was loaded last (D-NC33)', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [null]);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep']);
    const chooser = page.waitForEvent('filechooser');
    await page.locator('#clips-modal button', { hasText: '⤒ Restore' }).click();
    const file = { navicore: 'clip/v1', savedAt: 1, clip: { name: 'moded', durationMs: 200, mode: 3, events: BEEP } };
    await (await chooser).setFiles({ name: 'moded.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(file)) });
    await expect.poll(() => dialogs.length, { timeout: 20_000 }).toBe(1);
    expect(dialogs[0].message).toBe('Restored 1 clip(s).');
    expect(emu.clips.get('moded').events).toEqual(BEEP);
    expect(emu.clips.get('moded').mode, 'the restored clip\'s mode').toBe(3);
  });

  test('nctool.timeline_editor_save the timeline opens a clip through the verified download, saves exactly its model (indexed, one EDITEND), opens an incompletely-downloaded clip read-only, and closing mid-save cancels the upload and leaves the clip as it was', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [null]);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep']);
    await rowButton(page, 'wave', '📈').click();
    await expect(page.locator('#tl-modal')).toHaveClass(/open/);
    await expect(page.locator('#tl-status')).toHaveText('30 events · 2.8 s', { timeout: 20_000 });
    await expect(page.locator('#tl-save-btn')).toBeEnabled();

    // Edit the model the way the editor's handlers do, then Save: what lands is the flattened model, in order.
    const flat = await page.evaluate(() => {
      _tlClip.maestro['1-0'][3].pos += 100;
      _tlClip.actions.find((a) => a.action.type === 'wcb_unicast').action.cmd = ';S1edited';
      _tlClip.dirty = true;
      return _tlFlattenEvents();
    });
    const endsBefore = emu.lines(/^\?REC,EDITEND,/).length;
    await page.locator('#tl-save-btn').click();
    await expect(page.locator('#tl-status')).toHaveText('Saved ✓ (30 events)', { timeout: 20_000 });
    expect(emu.clips.get('wave').events).toEqual(flat.map(normEvent));
    expect(emu.clips.get('wave').events.find((e) => e.type === 'wcb_unicast').cmd).toBe(';S1edited');
    expect(emu.lines(/^\?REC,EDITEV,/).map((l) => +l.split(',')[2])).toEqual(flat.map((_, i) => i));
    expect(emu.lines(/^\?REC,EDITEND,/).slice(endsBefore)).toEqual(['?REC,EDITEND,wave']);
    await page.locator('#tl-close-btn').click();
    await expect(page.locator('#tl-modal')).not.toHaveClass(/open/);

    // An event that never arrives: the clip opens read-only, and Save refuses.
    emu.clipFault.dropAlways = new Set([1]);
    await rowButton(page, 'beep', '📈').click();
    await expect(page.locator('#tl-status')).toContainText('⚠ Incomplete — 1 of 3 events would not transfer. READ-ONLY', { timeout: 20_000 });
    await expect(page.locator('#tl-save-btn')).toBeDisabled();
    const begins = emu.lines(/^\?REC,EDITBEGIN/).length;
    await page.evaluate(() => _tlSave());
    await expect.poll(() => dialogs.length).toBe(1);
    expect(dialogs[0].message.split('\n')[0]).toBe('This clip downloaded incomplete and is open read-only.');
    expect(emu.lines(/^\?REC,EDITBEGIN/)).toHaveLength(begins);
    await page.locator('#tl-close-btn').click();
    emu.clipFault.dropAlways = new Set();

    // Close while the upload is waiting on an ACK: the tool cancels, and the clip on the board is the last saved one.
    const saved = emu.clips.get('wave').events.map((e) => ({ ...e }));
    emu.rec.loadedName = '';
    await rowButton(page, 'wave', '📈').click();
    await expect(page.locator('#tl-status')).toHaveText('30 events · 2.8 s', { timeout: 20_000 });
    await page.evaluate(() => { _tlClip.maestro['1-1'][0].pos = 4000; _tlClip.dirty = true; });
    emu.clipFault.holdFrom = 5;
    await page.locator('#tl-save-btn').click();
    await expect.poll(() => emu.lines(/^\?REC,EDITEV,5,/).length).toBe(1);
    await page.locator('#tl-close-btn').click();
    await expect(page.locator('#tl-modal')).not.toHaveClass(/open/);
    await expect.poll(() => emu.lines(/^\?REC,EDITCANCEL$/).length).toBeGreaterThan(0);
    expect(emu.rec.state).toBe('idle');
    expect(emu.clips.get('wave').events).toEqual(saved);
    expect(emu.lines(/^\?REC,EDITEND,/).slice(endsBefore)).toEqual(['?REC,EDITEND,wave']);
    T.expectNoPageErrors(page);
  });
});

test.describe('the Clips panel through a WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb', relayId: 1, clips: CLIPS() } });

  test('nctool.clips_list_forms relayed lists parse in both wire forms: per-item lines with log noise between them and a corrupt item dropped, and the old single marker hard-wrapped at 160 bytes and rejoined; the storage bar follows [CLIPFS]; an empty list says so', async ({ page, emu }) => {
    let form = 'items';
    emu.cli = (line, out, e) => {
      if (!/^\?REC,LS$/i.test(line)) return false;
      if (form === 'items') {
        e._recCli(line, (l) => {
          if (l === '[CLIPLIST:END]') out('[CLIPITEM]{"name":"broken","bytes":');       // a torn item: dropped
          out(l);
          if (l.startsWith('[CLIPITEM]')) out('[REC] a log line landing between items');
        }, true);
      } else if (form === 'old') {
        const clips = ['alpha_long_clip_name', 'bravo_long_clip_name', 'charlie_long_clip', 'delta_long_clip_nm']
          .map((name, i) => ({ name, bytes: 1000 + i, dur: 500 * (i + 1) }));
        out('[REC] clips:');
        out('[CLIPLIST]' + JSON.stringify({ clips }));                                   // > 160 B: two or more packets
      } else if (form === 'nofs') {
        out('[REC] clips:'); out('[CLIPLIST:BEGIN]'); out('[CLIPITEM]{"name":"solo","bytes":300,"dur":100,"n":2}'); out('[CLIPLIST:END]');
      } else {
        out('[CLIPFS]{"total":12582912,"used":0}'); out('[REC] clips:'); out('[CLIPLIST:BEGIN]'); out('[CLIPLIST:END]');
      }
      return true;
    };
    await T.openTool(page);
    await T.connectViaWcb(page);
    await openClips(page);
    expect(emu.lines(/^;w20,\?REC,LS$/).length).toBeGreaterThan(0);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep']);
    await expect(page.locator('#clips-storage')).toContainText('free of 12.00 MB');
    expect(await T.termLines(page, /a log line landing between items/)).toHaveLength(2);
    expect(await T.termLines(page, /CLIPITEM|CLIPLIST|CLIPFS/), 'markers never reach the terminal').toEqual([]);

    form = 'old';
    await page.locator('#clips-modal button', { hasText: '⟳ Refresh' }).click();
    await expect.poll(() => rowNames(page)).toEqual(['alpha_long_clip_name', 'bravo_long_clip_name', 'charlie_long_clip', 'delta_long_clip_nm']);
    await expect(page.locator('#clips-list')).toContainText('1.0 s · 1001 B');

    form = 'nofs';
    await page.locator('#clips-modal button', { hasText: '⟳ Refresh' }).click();
    await expect.poll(() => rowNames(page)).toEqual(['solo']);
    await expect(page.locator('#clips-storage')).toBeHidden();

    form = 'empty';
    await page.locator('#clips-modal button', { hasText: '⟳ Refresh' }).click();
    await expect(page.locator('#clips-empty')).toHaveText('No saved clips. Record one above, then Refresh.');
    T.expectNoPageErrors(page);
  });

  test('nctool.clip_restore_bridged over the WCB a clip whose event line would not fit one ESP-NOW packet is refused and the board left out of edit mode, while a small clip restores through [TERM:20] ACKs', async ({ page, emu }) => {
    // The longest action an event can carry: a 95-character command (cmd[96]), a delay and a 19-character note.
    const long = { name: 'longcmd', durationMs: 2759, mode: 1, events: [{ t: 2759, k: 0, type: 'wcb_unicast', target: '12',
                   cmd: ';' + 'X'.repeat(94), delay: 1500, note: 'nineteen chars note' }] };
    const small = { name: 'small', durationMs: 200, mode: 1, events: BEEP };
    const dialogs = T.answerDialogs(page, [null]);
    await T.openTool(page);
    await T.connectViaWcb(page);
    await openClips(page);
    await expect.poll(() => rowNames(page)).toEqual(['wave', 'beep']);
    const chooser = page.waitForEvent('filechooser');
    await page.locator('#clips-modal button', { hasText: '⤒ Restore' }).click();
    const file = { navicore: 'config-backup/v1', savedAt: 1, note: '', config: {}, clips: [long, small] };
    await (await chooser).setFiles({ name: 'b.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(file)) });
    await expect.poll(() => dialogs.length, { timeout: 30_000 }).toBe(1);
    const cmdLen = ('?REC,EDITEV,0,' + JSON.stringify(long.events[0])).length;
    expect(dialogs[0].message).toBe(`Restored 1 clip(s).\nFailed: longcmd (event 1/1 is too large for the WCB bridge (${cmdLen} chars) — restore over Direct USB)`);
    expect(cmdLen).toBeGreaterThan(180);
    expect(emu.lines(/^;w20,\?REC,EDITEV,0,\{"t":2759/)).toEqual([]);                     // never sent
    expect(emu.lines(/^;w20,\?REC,EDITCANCEL$/).length).toBeGreaterThan(0);
    expect(emu.clips.has('longcmd')).toBe(false);
    expect(emu.clips.get('small').events).toEqual(BEEP);
    expect(emu.rec.state).toBe('idle');
    T.expectNoPageErrors(page);
  });
});
