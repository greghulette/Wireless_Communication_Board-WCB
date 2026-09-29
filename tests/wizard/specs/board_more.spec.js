// Board tests for the Wizard's push side and its immediate editors (docs/hil_plan/WCB.md WCB-WP21). Each is started by
// the HIL harness test of the same id (tests/hil/suites/s30_wizard.py), which hands W1's COM port to Chrome, keeps W2
// on its own USB, binds the probe wires first, owns the before/after config check (config_guard) and undoes what the
// test wrote. hilCtx.args carries what the harness decided: markers, throwaway sequence keys, a variable name.
//
// Bench safety. Before any push, plannedVerbs (lib/wizard.js) checks what the push would send - verbs only - and the
// test stops if it is anything but what it meant to change. The only config writes are throwaway labels, sequences,
// variables and serial mappings, all undone (by the page and again by the harness), and ?HW with the value the board
// already has: a push the Wizard must reboot the board for, that changes nothing.
//
// Credentials. Nothing here reads a config value out of the page. Whole configs are compared inside the page and come
// back as the verbs of the commands that differ; recorded board lines come back only when they match a pattern that no
// config line matches (lib/wizard.js recordLines).
const { test, expect, hil } = require('../lib/fixtures');
const { openWizard, connectBoard, connectBoardDirect, manageRemote, recordLines, linesMatching, lineMark, plannedVerbs,
        setField, pushConfig } = require('../lib/wizard');

test.beforeEach(() => {
  test.skip(!hil.present, 'board tests run under the HIL harness: python tests/hil/run.py "wizard.*"');
});

const HW_OK = ['1', '21', '23', '24', '31', '32'];

// Toasts and the Push outcome, recorded in the page (a toast leaves the DOM after a few seconds).
async function watchToasts(page) {
  await page.evaluate(() => {
    window.__toasts = [];
    const t = window.showToast;
    window.showToast = (m, ty, d) => { window.__toasts.push(String(m)); return t(m, ty, d); };
  });
}
const toasts = (page) => page.evaluate(() => window.__toasts.join('\n'));

// The verbs of what would move the board's baseline back to `window.__before` (the config as pulled before the test's
// push): [] when the board came back exactly as it was.
function verbsSinceBefore(page, n) {
  return page.evaluate((n) => {
    const s = WCBParser.buildCommandString(boardBaselines[n], window.__before);
    return s ? s.split('^?').map((x, i) => (i === 0 ? x.slice(1) : x).split(',')[0]) : [];
  }, n);
}

// A push that needs a reboot and changes nothing: ?HW with the board's own version. Returns false (and the test skips)
// when the board reports none the builder would send.
async function sameHwPush(page, n) {
  const hw = await page.evaluate((n) => { window.__before = JSON.parse(JSON.stringify(boardBaselines[n])); return String(boardBaselines[n].hwVersion); }, n);
  test.skip(!HW_OK.includes(hw), `W${n} reports no hardware version the Wizard sends (${hw})`);
  await page.evaluate((n) => { boardBaselines[n].hwVersion = 0; }, n);
  expect(await plannedVerbs(page, n), 'the push carries ?HW alone').toEqual(['HW']);
}

test('wizard.push_reboot_path a push that needs a reboot, on the shared port the first board gets: the board reboots once, the page stays connected and reads its output throughout, and a pull afterwards finds the config as it was', async ({ page, hilCtx }) => {
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  expect(await page.evaluate((n) => !!boardConnections[n]._shared, n), 'boardManualConnect shares the first board').toBe(true);
  await recordLines(page, n);
  await watchToasts(page);
  await sameHwPush(page, n);
  const mark = await lineMark(page, n);
  const t0 = Date.now();
  expect(await pushConfig(page, n)).toMatchObject({ ok: true, aborted: false });
  const got = (re) => linesMatching(page, n, re, mark);
  // The board queues the restart and takes it once its command queue is quiet (4 s), or at the 20 s cap.
  await expect.poll(async () => (await got('^Booting up the ')).length, { timeout: 40_000 }).toBe(1);
  await expect.poll(async () => (await got('Raw Serial Forwarding Task Created')).length, { timeout: 20_000 }).toBe(1);
  const queued = await got('^Reboot queued');
  const now = await got('^Rebooting now');  // printed just before ESP.restart(): noted, not required
  const boot = await got('^Booting up the ');
  expect(queued.length, 'the board queued the restart it was asked for').toBe(1);
  await page.waitForTimeout(8000);          // nothing more: a second restart would print a second banner
  expect((await got('^Booting up the ')).length, 'one boot for one push').toBe(1);
  expect(await page.evaluate((n) => boardConnections[n].isConnected(), n)).toBe(true);
  // The Wizard does not pull on this path (W-14, pinned by wizard.push_fake_shared_reboot_repull): pull now, and compare.
  await page.evaluate((n) => { window.__b = boardBaselines[n]; return boardPull(n); }, n);
  await page.waitForFunction((n) => boardBaselines[n] !== window.__b && !_boardPullInFlight.has(n), n, { timeout: 30_000 });
  expect(await verbsSinceBefore(page, n), 'the board\'s config after the reboot').toEqual([]);
  expect(await toasts(page)).not.toContain('config pull incomplete');
  await hil.note(`push_reboot_path (shared): ?reboot queued ${queued[0].at - t0} ms after Push, ` +
                 `restart ${now.length ? `${now[0].at - t0} ms` : 'line not seen'}, banner ${boot[0].at - t0} ms`);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_reboot_path_direct a push that needs a reboot, on a direct connection: ?reboot, the port closed and reopened, the Wizard\'s own pull 3 s later lands whole and finds the config as it was, and the page ends connected', async ({ page, hilCtx }) => {
  await openWizard(page);
  const n = await connectBoardDirect(page, hilCtx);
  await recordLines(page, n);
  await watchToasts(page);
  await sameHwPush(page, n);
  await page.evaluate((n) => { window.__b = boardBaselines[n]; }, n);
  const t0 = Date.now();
  expect(await pushConfig(page, n)).toMatchObject({ ok: true, aborted: false });
  await page.waitForFunction((n) => boardBaselines[n] !== window.__b && !_boardPullInFlight.has(n) && boardConnections[n]?.isConnected(),
                             n, { timeout: 75_000 });
  const t1 = Date.now();
  expect(await verbsSinceBefore(page, n), 'the Wizard\'s pull after the reboot').toEqual([]);
  const tt = await toasts(page);
  expect(tt).not.toContain('config pull incomplete');
  expect(tt).not.toContain('did not come back');
  // a card shows its connection by its Connect button (updateConnectionUI, app.js); no card has a b<n>-conn-label
  expect(await page.evaluate((n) => document.getElementById(`b${n}-btn-connect`)?.textContent, n)).toBe('Disconnect');
  await hil.note(`push_reboot_path_direct: re-pulled ${t1 - t0} ms after Push; ` +
                 `${(await linesMatching(page, n, '^Booting up the ')).length} boot banner(s) seen by the page`);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_all_relay Push All with W1 on USB as the relay for W2: W2\'s label goes in one session before W1\'s own push, W1\'s reboot comes last, W1 comes back and is pulled, and both labels land (the harness checks and undoes them)', async ({ page, hilCtx }) => {
  const a = hilCtx.args;
  await openWizard(page);
  // Direct: Push All's last stage closes and reopens a relay, which a shared one cannot do (W-15).
  const n = await connectBoardDirect(page, hilCtx);
  expect(await manageRemote(page, n, a.target), `W${a.target} pulled through W${n}`).toBe(true);
  await watchToasts(page);
  await sameHwPush(page, n);                // first: it checks that ?HW is all W1 would send before the labels
  await setField(page, `#b${n}-s5-label`, a.label1);
  await setField(page, `#b${a.target}-s5-label`, a.label2);
  expect(await plannedVerbs(page, n), `W${n}'s push`).toEqual(['HW', 'LABEL']);
  expect(await plannedVerbs(page, a.target), `W${a.target}'s push`).toEqual(['LABEL']);
  // Every write to W1's port, in order, kept in the page; only the kind of each comes back.
  await page.evaluate(({ n, a }) => {
    const c = boardConnections[n];
    window.__order = [];
    const send = c.send.bind(c);
    c.send = (d, ...r) => { window.__order.push({ at: Date.now(), s: String(d).trim() }); return send(d, ...r); };
    window.__b = boardBaselines[n];
  }, { n, a });
  await page.evaluate(() => boardGoAll());
  await page.waitForFunction((n) => boardBaselines[n] !== window.__b && !_boardPullInFlight.has(n) && boardConnections[n]?.isConnected(),
                             n, { timeout: 75_000 });
  const kinds = await page.evaluate(({ a }) => window.__order.map((x) => (
    x.s.startsWith(`?MGMT,FRAG,${a.target},`) && x.s.endsWith(`?LABEL,S5,${a.label2}`) ? 'w2-label'
      : x.s === `?LABEL,S5,${a.label1}` ? 'w1-label' : x.s.startsWith('?HW,') ? 'w1-hw' : x.s === '?reboot' ? 'reboot' : 'other')), { a });
  const first = (k) => kinds.indexOf(k);
  expect([first('w2-label'), first('w1-hw'), first('w1-label'), first('reboot')].every((i) => i >= 0), kinds.join(',')).toBe(true);
  expect(first('w2-label') < first('w1-hw') && first('w1-label') < first('reboot'), kinds.filter((k) => k !== 'other').join(',')).toBe(true);
  expect(kinds.filter((k) => k === 'reboot').length).toBe(1);
  expect(await page.evaluate(({ n, t }) => [boardPushOutcome[t], boardPushOutcome[n]], { n, t: a.target }))
    .toEqual([{ ok: true, aborted: false, reason: '' }, { ok: true, aborted: false, reason: '' }]);
  const tt = await toasts(page);
  expect(tt).not.toContain('did not come back');
  expect(tt).not.toContain('config pull incomplete');
  expect(await page.evaluate(() => generalSettingsDirty)).toBe(false);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.mapping_bidir_relay the mapping editor on W1 with W2 behind it: Save with bidir puts W1 S2 -> W2 S4 on W1 and the reverse on W2 at once, lines flow both ways through the probes, and Remove clears W1\'s (the harness checks W2\'s: W-21)', async ({ page, hilCtx }) => {
  const a = hilCtx.args;
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  expect(await manageRemote(page, n, a.target)).toBe(true);
  // Wizard slots are WCB numbers (n, a.target); hil.wire() takes the bench's board index (1 = W1, 2 = W2).
  const rowId = await page.evaluate((n) => { addMappingRow(n); return [...document.querySelectorAll(`#b${n}-mappings-container .mapping-card`)].at(-1).id; }, n);
  const sel = (id, v) => page.evaluate(({ id, v }) => {
    const e = document.getElementById(id);
    if (e.type === 'checkbox') e.checked = v; else e.value = v;
    e.dispatchEvent(new Event('change', { bubbles: true }));
  }, { id, v });
  await sel(`${rowId}-src`, '2');
  // The source list leaves out a port a device claims (refreshMappingSourceDropdown): then there is nothing to test.
  test.skip(await page.evaluate((id) => document.getElementById(id).value !== '2', `${rowId}-src`),
            `W${n} S2 is claimed by a device in its config, so the mapping editor does not offer it as a source`);
  const destId = await page.evaluate(({ rowId, n }) => { addMappingDestination(rowId, n); return [...document.querySelectorAll(`#${rowId}-destinations .map-dest-row`)].at(-1).id; }, { rowId, n });
  await sel(`${destId}-wcb`, String(a.target));
  await sel(`${destId}-port`, '4');
  await sel(`${rowId}-bidir`, true);
  expect(await page.evaluate((id) => [...document.querySelectorAll(`#${id} select`)].map((s) => s.value), destId)).toEqual([String(a.target), '4']);
  await page.evaluate(({ rowId, n }) => saveMappingRow(rowId, n), { rowId, n });
  await page.waitForTimeout(2500);          // the reverse rides a FRAG through W1 to W2
  const w1s2 = hil.wire(1, 'S2');
  const w2s4 = hil.wire(a.target, 'S4');
  const prime = async (w) => { await w.send('\r'); await page.waitForTimeout(300); };
  await prime(w1s2);
  let m = await w2s4.mark();
  await w1s2.send(`${a.m1}\r`);
  await w2s4.expect(`${a.m1}\r`, m, 4);
  await prime(w2s4);
  m = await w1s2.mark();
  await w2s4.send(`${a.m2}\r`);
  await w1s2.expect(`${a.m2}\r`, m, 4);
  // saveMappingRow pulls W1 2 s later (app.js), which renders the mapping cards again under new ids - and a row's bidir
  // link lives only in the page, so the pulled card has none. Remove the card that shows S2 now, as a person would.
  await page.waitForFunction((n) => !_boardPullInFlight.has(n), n, { timeout: 15_000 });
  const liveRow = await page.evaluate((n) => [...document.querySelectorAll(`#b${n}-mappings-container .mapping-card`)]
    .find((c) => document.getElementById(`${c.id}-src`)?.value === '2')?.id ?? null, n);
  expect(liveRow).not.toBeNull();
  await page.evaluate(({ rowId, n }) => removeMappingRow(rowId, n), { rowId: liveRow, n });
  await page.waitForTimeout(1500);
  await hil.note('mapping_bidir_relay: W1 S2 <-> W2 S4 carried both ways; W1\'s side removed through the editor');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.seq_var_editors the sequence and variable editors on W1 and on W2 through it: save, rename, test and remove a throwaway sequence (one over 198 characters to W2, as a multi-part session), each checked by ?SEQ,NAMES / ?SEQ,GET / ?MGMT,SEQGET; a variable set and cleared, checked by ?VAR,LIST, directly and through the relay', async ({ page, hilCtx }) => {
  const a = hilCtx.args;
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  expect(await manageRemote(page, n, a.target)).toBe(true);
  // One reply line of the kind asked for, read in the page: only [MGMT:SEQ...] lines and ?VAR,LIST rows come back.
  const ask = (cmd, re) => page.evaluate(async ({ n, cmd, re }) => {
    const raw = await boardConnections[n].sendAndCollect(cmd, 5000, re.replace(/\\/g, '').replace(/^\^/, ''));
    return raw.split('\n').find((l) => new RegExp(re).test(l)) ?? null;
  }, { n, cmd, re });
  const names = async (wcb) => {
    const l = await ask(wcb === n ? '?SEQ,NAMES' : `?MGMT,SEQ,${wcb}`, `^\\[MGMT:SEQ,${wcb}\\]`);
    return l ? l.replace(/^\[MGMT:SEQ,\d+\]/, '').split(',').slice(2) : null;
  };
  const value = async (wcb, key) => {
    const l = await ask(wcb === n ? `?SEQ,GET,${key}` : `?MGMT,SEQGET,${wcb},${key}`, `^\\[MGMT:SEQVAL,${wcb}\\]${key},`);
    return l ? l.replace(/^\[MGMT:SEQVAL,\d+\][^,]*,/, '') : null;
  };
  const setRow = (sel, v) => page.evaluate(({ sel, v }) => { const e = document.querySelector(sel); e.value = v; e.dispatchEvent(new Event('input', { bubbles: true })); }, { sel, v });
  const lastRow = (tbody) => page.evaluate((tbody) => [...document.querySelectorAll(`#${tbody} tr`)].at(-1).id, tbody);

  // W1 directly: save, test (the probe on W1 S1 sees its ;S1 output), rename, remove.
  await page.evaluate((n) => addSequenceRow(n), n);
  const r1 = await lastRow(`b${n}-seq-tbody`);
  await setRow(`#${r1} .seq-key-input`, a.key1);
  await setRow(`#${r1} .seq-val-textarea`, `;S1${a.m1}`);
  await page.evaluate(({ n, id }) => updateSequence(n, id), { n, id: r1 });
  await page.waitForTimeout(800);
  expect(await names(n)).toContain(a.key1);
  expect(await value(n, a.key1)).toBe(`OK,;S1${a.m1}`);
  const s1 = hil.wire(1, 'S1');             // the bench's W1 (a board index, not a WCB number)
  const m = await s1.mark();
  await page.evaluate(({ n, id }) => playSequence(n, id), { n, id: r1 });
  await s1.expect(`${a.m1}\r`, m, 4);
  await setRow(`#${r1} .seq-key-input`, a.key2);
  await page.evaluate(({ n, id }) => updateSequence(n, id), { n, id: r1 });
  await page.waitForTimeout(800);
  const now = await names(n);
  expect([now.includes(a.key1), now.includes(a.key2)], 'renamed').toEqual([false, true]);
  await page.evaluate(({ n, id }) => removeSequenceRow(n, id), { n, id: r1 });
  await page.waitForTimeout(800);
  expect(await names(n)).not.toContain(a.key2);

  // W2 through the relay: a value too long for one packet goes in parts, and arrives exact.
  const long = `;S2${a.m2}${'L'.repeat(200)}`;
  await page.evaluate((t) => addSequenceRow(t), a.target);
  const r2 = await lastRow(`b${a.target}-seq-tbody`);
  await setRow(`#${r2} .seq-key-input`, a.key3);
  await setRow(`#${r2} .seq-val-textarea`, long);
  await page.evaluate(({ t, id }) => updateSequence(t, id), { t: a.target, id: r2 });
  await expect.poll(() => value(a.target, a.key3), { timeout: 12_000 }).toBe(`OK,${long}`);
  await page.evaluate(({ t, id }) => removeSequenceRow(t, id), { t: a.target, id: r2 });
  await expect.poll(async () => (await names(a.target))?.includes(a.key3), { timeout: 12_000 }).toBe(false);

  // A variable, on W1 and on W2, read back through ?VAR,LIST (directly, and as W2's [TERM:n] lines).
  for (const [wcb, v] of [[n, 7], [a.target, 5]]) {
    await page.evaluate((w) => addVariableRow(w), wcb);
    const vr = await lastRow(`b${wcb}-var-tbody`);
    await setRow(`#${vr} .var-name-input`, a.var);
    await setRow(`#${vr} .var-value-input`, String(v));
    await page.evaluate(({ w, id }) => updateVariable(w, id), { w: wcb, id: vr });
    const listed = () => page.evaluate(async ({ w, name }) => parseVarList(await collectVarList(w, 5000)).find((x) => x.name === name) ?? null,
                                       { w: wcb, name: a.var });
    await expect.poll(listed, { timeout: 12_000 }).toEqual({ name: a.var, value: v, persist: true });
    await page.evaluate(({ w, id }) => removeVariableRow(w, id), { w: wcb, id: vr });
    await expect.poll(listed, { timeout: 12_000 }).toBe(null);
  }
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wdp_da_forget a device announcing on W2 S4 shows in the W1-connected mesh panel, and its Forget button removes it from W2 through W1 (the harness checks W2\'s own list)', async ({ page, hilCtx }) => {
  const a = hilCtx.args;
  await openWizard(page);
  await connectBoard(page, hilCtx);
  const btn = `.wdp-btn-forget[data-n="${a.target}"][data-s="4"][data-type="${a.name}"]`;
  const refreshShows = async () => { await page.evaluate(() => wdpMeshRefresh()); return page.locator(btn).count(); };
  // Chrome's open resets W1, whose record of W2's devices is RAM only, and a board hears its neighbours' device lists
  // after their next advert - up to the 60 s backstop unless asked. The panel's Poll mesh (?WDP,POLL) asks every board.
  await page.evaluate(() => wdpPollMesh());
  await expect.poll(refreshShows, { timeout: 20_000 }).toBe(1);
  page.on('dialog', (d) => d.accept());     // wdpForgetDevice asks first
  await page.locator(btn).click();
  await expect.poll(refreshShows, { timeout: 30_000 }).toBe(0);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.relay_terminal a remote board\'s terminal pane through W1: ;S2 typed there comes out of W2 S2, ?VERSION typed there answers in that pane, and disconnecting W1 leaves W2 unmanaged', async ({ page, hilCtx }) => {
  const a = hilCtx.args;
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  expect(await manageRemote(page, n, a.target)).toBe(true);
  const type = (cmd) => page.evaluate(({ t, cmd }) => { document.getElementById(`term-pane-input-${t}`).value = cmd; return sendTerminalCommandTo(t); },
                                      { t: a.target, cmd });
  const w2s2 = hil.wire(a.target, 'S2');
  const m = await w2s2.mark();
  await type(`;S2${a.m1}`);
  await w2s2.expect(`${a.m1}\r`, m, 5);
  await page.evaluate((t) => clearTerminalPane(t), a.target);
  await type('?VERSION');
  await expect(page.locator(`#term-pane-output-${a.target}`)).toContainText('Software Version:', { timeout: 10_000 });
  await page.evaluate((n) => boardDisconnect(n), n);
  expect(await page.evaluate((t) => [remoteRelayForBoard[t] ?? null, document.getElementById(`b${t}-status-badge`)?.textContent,
                                     document.getElementById(`b${t}-btn-connect`)?.textContent], a.target))
    .toEqual([null, 'Not Connected', 'Connect']);
  expect(page.wizErrors).toEqual([]);
});
