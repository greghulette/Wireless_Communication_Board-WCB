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

test('wizard.var_fake_temporary_rows a temporary variable row SETs with ;V,name,value and never enters the config; its REMOVE clears it on the board with ?VAR,CLEAR,<name> (through the relay for a managed board) and leaves the config and baseline alone; one never set sends nothing', async ({ page }) => {
  // appendTempVariableRow, updateTempVariable, clearVariableRow (app.js:5131-5221). The ;V set alone is
  // wizard.editors_fake_seq_var's. Row var.temporary_rows.
  await setup(page);
  await pullFake(page, 1, board(['VAR,SET,lights,3']));
  await holdVerifyPulls(page);
  const stores = () => page.evaluate(() => JSON.stringify([boardConfigs[1].variables, boardBaselines[1].variables]));
  const before = await stores();
  await page.evaluate(() => { __fake.log.length = 0; addTemporaryVariableRow(1); });
  const row = '#b1-var-tbody tr[data-var-type="temporary"]';
  await edit(page, `${row} .var-name-input`, 'tmp1');
  await edit(page, `${row} .var-value-input`, '5');
  await page.evaluate((row) => document.querySelector(`${row} [title="Save/Update Variable"]`).click(), row);
  await expect.poll(() => sentOn(page, 1)).toEqual([';V,tmp1,5']);
  expect(await page.evaluate((row) => document.querySelector(`${row} [title="Remove Variable"]`).textContent, row)).toBe('REMOVE');
  const m = await toastMark(page);
  await page.evaluate((row) => document.querySelector(`${row} [title="Remove Variable"]`).click(), row);
  await expect.poll(() => sentOn(page, 1)).toEqual([';V,tmp1,5', '?VAR,CLEAR,tmp1']);
  expect(await page.evaluate((row) => document.querySelector(row), row), 'the row is gone').toBe(null);
  expect(await toastsSince(page, m)).toContain('Variable "tmp1" cleared on WCB 1');
  expect(await stores(), 'config and baseline untouched').toBe(before);
  expect((await push(page, 1, { skipReboot: true })).outcome.reason).toBe('no changes to push');

  // A temporary row that was never set: REMOVE only drops it.
  await page.evaluate(() => { __fake.log.length = 0; addTemporaryVariableRow(1); });
  await page.evaluate((row) => document.querySelector(`${row} [title="Remove Variable"]`).click(), row);
  expect(await sentOn(page, 1)).toEqual([]);
  expect(await page.evaluate((row) => document.querySelector(row), row)).toBe(null);

  // Through a relay: the clear is a one-chunk FRAG to the board.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await page.evaluate(() => { __fake.log.length = 0; appendTempVariableRow(2, 'livev', 9, true); });
  expect(await page.evaluate(() => document.querySelector('#b2-var-tbody tr.var-row-live [title="Remove Variable"]').textContent))
    .toBe('CLEAR');
  await page.evaluate(() => document.querySelector('#b2-var-tbody tr.var-row-live [title="Remove Variable"]').click());
  await expect.poll(async () => (await sentOn(page, 1)).map(frag)).toEqual(['?MGMT,FRAG,2,SID,0,1,?VAR,CLEAR,livev']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.var_fake_refresh_list Refresh on a USB board sends ?VAR,LIST, reads the board\'s lines to the "used" footer, puts a changed value into the existing row and into the config and baseline (so a push has nothing to send), keeps a value being edited, and adds a persistent variable not in the config read-only and a volatile one as a temporary row', async ({ page }) => {
  // refreshVariablesFromBoard, collectVarList (direct), parseVarList, renderLiveVariables (app.js:5351-5480). The relay
  // path is wizard.editors_fake_seq_var's. Row var.refresh_list.
  await setup(page);
  await pullFake(page, 1, board(['VAR,SET,lights,3', 'VAR,SET,speed,1']));
  await holdVerifyPulls(page);
  // The board answers ?VAR,LIST on its stream, as the firmware prints it.
  await page.evaluate(() => {
    const c = boardConnections[1];
    const send = c.send;
    window.__varList = ['Variables:', '  lights = 7  [persistent]', '  speed = 4  [persistent]', '  extra = 2  [persistent]',
                        '  tmpx = -4  [volatile]', '4/100 used', '  late = 1  [persistent]'];
    c.send = async (s) => {
      await send(s);
      if (/^\?VAR,LIST/.test(s)) setTimeout(() => window.__varList.forEach((l) => c._handleLine(l)), 50);
    };
    __fake.log.length = 0;
  });
  // The user is mid-edit on speed: that input keeps what they typed.
  await page.evaluate(() => {
    const row = [...document.querySelectorAll('#b1-var-tbody tr')].find((r) => r.querySelector('.var-name-input')?.value === 'speed');
    const v = row.querySelector('.var-value-input');
    v.value = '6';
    v.focus();
  });
  const m = await toastMark(page);
  await page.evaluate(() => refreshVariablesFromBoard(1));
  expect(await sentOn(page, 1)).toEqual(['?VAR,LIST']);
  const rows = await page.evaluate(() => [...document.querySelectorAll('#b1-var-tbody tr')].map((r) => ({
    name: r.querySelector('.var-name-input')?.value ?? r.cells[0].textContent.trim(),
    value: r.querySelector('.var-value-input')?.value ?? r.cells[1].textContent.trim(),
    live: r.classList.contains('var-row-live'), temp: r.dataset.varType === 'temporary', note: r.cells[3].textContent.trim(),
  })));
  expect(rows.map(({ name, value, live, temp }) => ({ name, value, live, temp }))).toEqual([
    { name: 'lights', value: '7', live: false, temp: false },
    { name: 'speed', value: '6', live: false, temp: false },
    { name: 'extra', value: '2', live: true, temp: false },
    { name: 'tmpx', value: '-4', live: true, temp: true },
  ]);
  expect(rows[2].note).toBe('on board — not in config');
  expect(await page.evaluate(() => [boardConfigs[1].variables, boardBaselines[1].variables].map((vs) => vs.map((v) => `${v.name}=${v.value}`))))
    .toEqual([['lights=7', 'speed=4'], ['lights=7', 'speed=4']]);
  expect(await toastsSince(page, m)).toContain('WCB 1: 4 variable(s) on board — 3 persistent, 1 temporary');
  // Pushed: lights is the board's value now (no ?VAR,SET,lights); speed carries the user's typed 6.
  await page.evaluate(() => document.activeElement.blur());
  expect((await push(page, 1, { skipReboot: true })).sent).toEqual(['1:?VAR,SET,speed,6']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.term_fake_drawer the Terminal button opens the drawer, pads the page above it and scrolls every pane to its newest line; dragging its edge resizes it between 120 px and 80% of the window; Clear All empties every pane; Close closes it and drops the padding', async ({ page }) => {
  // toggleTerminal, syncMainPadding, initTerminalResize (app.js:9687-9760), clearAllTerminals (9902-9904). The scroll
  // on open looked for '.term-output', a class no pane has, so a pane filled while the drawer was shut opened at its
  // oldest line: fixed 2026-10-13 ('.terminal-pane-output'). Row term.drawer.
  await setup(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([], { wcb: 2 }));
  await page.evaluate(() => { for (let i = 0; i < 200; i++) { termLog(1, `line ${i}`); termLog(2, `other ${i}`); } });
  const st = () => page.evaluate(() => {
    const d = document.getElementById('terminal-drawer');
    const outs = [...document.querySelectorAll('[id^="term-pane-output-"]')];
    return { open: d.classList.contains('open'), height: d.offsetHeight, pad: document.querySelector('.main').style.paddingBottom,
             atBottom: outs.map((o) => o.scrollHeight > o.clientHeight && o.scrollTop + o.clientHeight >= o.scrollHeight - 2),
             lines: outs.map((o) => o.childElementCount) };
  });
  await page.locator('#btn-terminal').click();
  let s = await st();
  expect(s).toMatchObject({ open: true, pad: `${s.height + 24}px`, atBottom: [true, true] });
  expect(s.lines.every((k) => k >= 200)).toBe(true);

  const drag = async (dy) => {
    const b = await page.locator('#terminal-resize-handle').boundingBox();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2 + dy, { steps: 6 });
    await page.mouse.up();
  };
  const vh = await page.evaluate(() => window.innerHeight);
  await drag(-2000);
  s = await st();
  expect(s.height, 'capped at 80% of the window').toBe(Math.round(vh * 0.8));
  expect(s.pad).toBe(`${s.height + 24}px`);
  await drag(+2000);
  expect((await st()).height, 'floored at 120 px').toBe(120);

  await page.locator('#terminal-drawer button', { hasText: 'Clear All' }).click();
  expect((await st()).lines).toEqual([0, 0]);
  await page.locator('#terminal-drawer button', { hasText: 'Close' }).click();
  expect(await st()).toMatchObject({ open: false, pad: '' });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.term_fake_panes each managed board gets a pane whose visibility chip says how it is reached - "WCB 1 (USB)", "WCB 2 (via WCB 1)" - and whose input is disabled ("Board disconnected") while the board is not reachable; a board that moves slot takes its pane\'s output with it and the old pane goes', async ({ page }) => {
  // ensureTerminalPane, updatePaneVisibilityChip, updateTerminalPaneDot (app.js:9862-9945), migrateTerminalPane
  // (9965-9975). Visibility and the debug reset are wizard.app_fake_terminal's. Row term.panes.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  const chip = (n) => page.evaluate((n) => document.getElementById(`term-vis-label-${n}`)?.textContent ?? null, n);
  expect([await chip(1), await chip(2)]).toEqual(['WCB 1 (USB)', 'WCB 2 (via WCB 1)']);
  const input = (n) => page.evaluate((n) => {
    const i = document.getElementById(`term-pane-input-${n}`);
    return [i.disabled, i.placeholder, document.getElementById(`term-pane-header-${n}`).classList.contains('disconnected')];
  }, n);
  expect(await input(2)).toEqual([false, 'Send to WCB 2…', false]);
  await page.evaluate(() => updateTerminalPaneDot(2, false));
  expect(await input(2)).toEqual([true, 'Board disconnected', true]);
  await page.evaluate(() => updateTerminalPaneDot(2, true));
  expect(await input(2)).toEqual([false, 'Send to WCB 2…', false]);
  // Un-managed, then connected over USB: the same pane, its chip now reading USB.
  await page.evaluate(() => { clearRemoteConnected(2); __fake.conn(2, { backup: '' }); updateConnectionUI(2, true); });
  expect(await chip(2)).toBe('WCB 2 (USB)');
  expect(await page.evaluate(() => document.querySelectorAll('#term-pane-2').length)).toBe(1);

  // A board landing in slot 4 that is WCB 6: its output moves to pane 6, pane 4 and its chip go.
  await page.evaluate(() => { ensureTerminalPane(4); termLog(4, 'hil-before-the-move'); });
  await pullFake(page, 4, board([], { wcb: 6, wcbq: 6 }));
  expect(await page.evaluate(() => [!!document.getElementById('term-pane-4'), !!document.getElementById('term-vis-chip-4'),
                                    document.getElementById('term-pane-output-6').textContent.includes('hil-before-the-move')]))
    .toEqual([false, false, true]);
  expect(await chip(6)).toBe('WCB 6 (USB)');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.term_fake_send_history Enter sends a terminal line and keeps it in that board\'s history (a repeat of the last entry once); ArrowUp walks back through it and stops at the oldest, ArrowDown walks forward to an empty line; each board has its own', async ({ page }) => {
  // onTerminalKeydown, sendTerminalCommandTo's history (app.js:10060-10120). The sends themselves are
  // wizard.app_fake_terminal's and wizard.relay_terminal's. Row term.send_history.
  await setup(page);
  await pullFake(page, 1, board());
  await pullFake(page, 2, board([], { wcb: 2 }));
  await page.locator('#btn-terminal').click();
  await page.evaluate(() => { __fake.log.length = 0; });
  const in1 = page.locator('#term-pane-input-1');
  for (const cmd of ['?WCB', '?VERSION', '?VERSION', '?ETM,LIST']) {
    await in1.fill(cmd);
    await in1.press('Enter');
  }
  expect(await sentOn(page, 1)).toEqual(['?WCB', '?VERSION', '?VERSION', '?ETM,LIST']);
  expect(await page.evaluate(() => boardCmdHistory[1])).toEqual(['?WCB', '?VERSION', '?ETM,LIST']);
  expect(await in1.inputValue(), 'cleared after a send').toBe('');
  const walk = [];
  for (const k of ['ArrowUp', 'ArrowUp', 'ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowDown', 'ArrowDown']) {
    await in1.press(k);
    walk.push(await in1.inputValue());
  }
  expect(walk).toEqual(['?ETM,LIST', '?VERSION', '?WCB', '?WCB', '?VERSION', '?ETM,LIST', '', '']);
  // WCB2's history is its own: empty, so the arrows leave its input alone.
  const in2 = page.locator('#term-pane-input-2');
  await in2.fill('typed');
  await in2.press('ArrowUp');
  expect(await in2.inputValue()).toBe('typed');
  await in2.press('Enter');
  await in1.press('ArrowUp');
  expect(await in1.inputValue()).toBe('?ETM,LIST');
  expect(await page.evaluate(() => boardCmdHistory[2])).toEqual(['typed']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.term_fake_log_routing a line for a board with a connection, a relay or a pane opened by hand goes to that board\'s pane (made if need be); one for a board only discovered on the mesh gets no pane and lands in the first pane; with no pane at all it is dropped', async ({ page }) => {
  // termLog (app.js:10010-10050). Row term.log_routing.
  await setup(page);
  const panes = () => page.evaluate(() => [...document.querySelectorAll('#terminal-panes .terminal-pane')].map((p) => +p.id.slice(10)));
  await page.evaluate(() => termLog(0, 'hil-nowhere'));
  expect(await panes(), 'no pane yet: dropped, nothing made').toEqual([]);
  await pullFake(page, 1, board());
  expect(await panes()).toEqual([1]);
  await page.evaluate(() => { addDiscoveredBoards([7]); termLog(7, 'hil-from-a-discovered-board'); });
  expect(await panes(), 'no pane for a board only heard on the mesh').toEqual([1]);
  expect(await page.evaluate(() => document.getElementById('term-pane-output-1').textContent)).toContain('hil-from-a-discovered-board');
  // Managed through a relay: its own pane.
  await page.evaluate(() => { addDiscoveredBoards([8]); remoteRelayForBoard[8] = 1; termLog(8, 'hil-relayed'); });
  expect(await panes()).toEqual([1, 8]);
  // A connection object (even one not open yet): its own pane.
  await page.evaluate(() => { boardConnections[9] = new BoardConnection(9); termLog(9, 'hil-conn'); });
  expect(await panes()).toEqual([1, 8, 9]);
  // A pane opened by hand (the relay card's Terminal button) takes that board's lines.
  await page.evaluate(() => { ensureTerminalPane(12); termLog(12, 'hil-by-hand'); });
  expect(await page.evaluate(() => [8, 9, 12].map((n) => document.getElementById(`term-pane-output-${n}`).textContent)))
    .toEqual(['hil-relayed', 'hil-conn', 'hil-by-hand']);
  expect(await page.evaluate(() => document.getElementById('term-pane-output-1').textContent)).not.toContain('hil-relayed');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.term_fake_remote_keepalive while a board stays managed through a relay its remote terminal is re-armed - one ?RTERM,START,<relay> 12 s after the last start, so the firmware\'s 30 s idle timeout never ends it and no minute holds more than 6 - and its echo of a re-arm stays out of its pane; a board the relay does not manage gets none; a push on the relay holds a re-arm back, and un-managing the board or losing the relay stops them', async ({ page }) => {
  // startRemoteTermSession, _rtermKeepAliveArm / _rtermKeepAliveTick / _rtermKeepAliveEcho, clearRemoteConnected (app.js).
  // The firmware now ends a remote-terminal session not re-armed for 30 s (WCB 3b9e2dd, WCBSerial::expireIfIdle); the
  // Wizard armed a session once and never again. Every one-chunk FRAG copy runs on the board as its own ETM unicast,
  // so each send below is a start the board runs; s35_intellex_wifi.py bounds those at 6 a minute (RATE_BOUND). Time is
  // Playwright's fake clock. New 2026-10-13, for RESCAN_2026-10-12.md section 3's "remote-terminal sessions have no
  // timeout"; its stop is part of rows remote.unmanage and connect.disconnect.
  await page.clock.install();
  await setup(page);
  await page.evaluate(() => {
    __fake.conn(1, { backup: '' });     // the relay, on USB
    addDiscoveredBoards([2, 3]);
    setRemoteConnected(2, 1);           // WCB2 managed through WCB1; WCB3 only seen on the mesh
    window.__starts = (n) => __fake.log.filter((e) => e.slot === 1 && e.op === 'send' && e.s.includes(`MGMT,FRAG,${n},`) &&
                                                    e.s.endsWith('?RTERM,START,1')).map((e) => ({ at: e.at, sid: e.s.split(',')[3] }));
    window.__echo = () => boardConnections[1]._handleLine('[TERM:2][RTERM] Session started → relay WCB1');
    window.__echoes = () => (document.getElementById('term-pane-output-2').textContent.match(/\[RTERM\] Session started/g) || []).length;
  });
  await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
  await page.clock.runFor(2000);        // the arming: three copies of one start, 250 ms apart
  const arming = await page.evaluate(() => __starts(2));
  expect(arming.length).toBe(3);
  expect(new Set(arming.map((x) => x.sid)).size).toBe(1);
  // The arming's own announcement shows in WCB2's pane.
  await page.evaluate(() => __echo());
  expect(await page.evaluate(() => __echoes())).toBe(1);
  // A start sent for WCB3, which the relay does not manage (a pull through the relay arms one): no keep-alive follows.
  await page.evaluate(() => { startRemoteTermSession(1, 3); });   // not awaited: its 250 ms gaps run on the paused clock
  await page.clock.runFor(1000);
  const wcb3 = (await page.evaluate(() => __starts(3))).length;
  expect(wcb3).toBe(3);

  // Two minutes: a single copy every 12 s after the arming's last one, each a fresh session.
  await page.clock.runFor(120_000);
  const all = await page.evaluate(() => __starts(2));
  const kept = all.slice(3);
  expect(kept.length).toBe(10);
  expect(new Set(kept.map((x) => x.sid)).size, 'one copy each').toBe(10);
  const gaps = [kept[0].at - arming[2].at, ...kept.slice(1).map((x, i) => x.at - kept[i].at)];
  for (const g of gaps) expect(g).toBe(12_000);
  // At most 6 in any 60 s window once the arming is past (the s35 bound): a window opening at each re-arm.
  for (const x of kept) expect(kept.filter((y) => y.at >= x.at && y.at <= x.at + 60_000).length).toBeLessThanOrEqual(6);
  expect((await page.evaluate(() => __starts(3))).length, 'WCB3: nothing after its one arming').toBe(wcb3);
  // The board's announcement of a re-arm is kept out of its pane - once per re-arm; any other still shows.
  await page.evaluate(() => __echo());
  expect(await page.evaluate(() => __echoes())).toBe(1);
  await page.evaluate(() => __echo());
  expect(await page.evaluate(() => __echoes())).toBe(2);
  // Nothing in the relay's pane for a re-arm (the arming says "remote terminal started" once).
  expect(await page.evaluate(() => (document.getElementById('term-pane-output-1').textContent.match(/WCB2 remote terminal started/g) || []).length)).toBe(1);

  // A push on the relay: the next re-arm waits until it is over (checked every second), then the 12 s run on from it.
  const last = kept.at(-1).at;
  await page.evaluate(() => _pushingBoards.add(1));
  await page.clock.runFor(last + 15_000 - (await page.evaluate(() => Date.now())));
  expect((await page.evaluate(() => __starts(2))).length, 'held back during the push').toBe(13);
  await page.evaluate(() => _pushingBoards.delete(1));
  await page.clock.runFor(1_000);
  const after = (await page.evaluate(() => __starts(2))).slice(13);
  expect(after.length).toBe(1);
  expect(after[0].at - last).toBeGreaterThanOrEqual(15_000);
  expect(after[0].at - last).toBeLessThanOrEqual(16_000);
  // A start from another path (a pull that landed re-arms the terminal) puts the next re-arm 12 s after it.
  await page.clock.runFor(5_000);
  await page.evaluate(() => { startRemoteTermSession(1, 2); });
  await page.clock.runFor(1_000);
  const pulled = (await page.evaluate(() => __starts(2))).slice(14);
  expect(pulled.length).toBe(3);
  await page.clock.runFor(12_000);
  const next = (await page.evaluate(() => __starts(2))).slice(17);
  expect(next.length).toBe(1);
  expect(next[0].at - pulled[2].at).toBe(12_000);

  // Un-managed: RTERM,STOP, and no re-arm after it.
  await page.evaluate(() => clearRemoteConnected(2));
  await page.clock.runFor(1_000);
  expect(await page.evaluate(() => __fake.sent(1).filter((s) => s.includes('MGMT,FRAG,2,') && s.endsWith('RTERM,STOP')).length)).toBe(1);
  const stopped = (await page.evaluate(() => __starts(2))).length;
  await page.clock.runFor(60_000);
  expect((await page.evaluate(() => __starts(2))).length, 'none once un-managed').toBe(stopped);

  // Managed again, then the relay's port is gone: the re-arms stop.
  await page.evaluate(() => setRemoteConnected(2, 1));
  await page.clock.runFor(1_000);
  const rearmed = (await page.evaluate(() => __starts(2))).length;
  expect(rearmed).toBe(stopped + 3);
  await page.evaluate(() => { boardConnections[1]._connected = false; });
  await page.clock.runFor(60_000);
  expect((await page.evaluate(() => __starts(2))).length, 'none with the relay gone').toBe(rearmed);
  expect(await page.evaluate(() => Object.keys(_rtermKeepAlive))).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});
