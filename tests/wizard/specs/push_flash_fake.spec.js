// Needs no board: the push guards and the flash / erase paths of boardGo (Wizard/app.js), the gaps the 2026-10-12
// coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json).
// Fake boards in the real page (lib/fake.js): boardPull, boardGo, boardGoRemote and exportSystemFile run as on the
// bench, and only the wire is recorded. For the flash paths window.flashFirmware is a recorder (the flasher itself is
// flasher_fake.spec.js), the fake gets a port object for boardGo to hand it and close, and a board that answers
// waitForBoardReady's ?backup probe with the backup's end marker. Runs standalone, in CI, and under the harness by id
// (s30_wizard.py).
//
// Nothing here is a real credential: the mesh password is lib/fake.js's dummy, and nothing prints a pushed chain.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, chain, install, pullFake, push, edit, relayFake } = require('../lib/fake');

// The Wizard asks GitHub for the latest release on load, and a successful flash stamps that version into the board's
// config: a fixed answer keeps every run the same, and nothing leaves the machine.
async function setup(page) {
  await page.route('https://api.github.com/**', (route) => route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify([{ type: 'file', name: 'WCB_6.2.3_010101ROCT2026_WIFI_ESP32.bin', download_url: 'https://fake.invalid/b' }]) }));
  await openWizard(page);
  await install(page);
}

// What exportSystemFile would download (as app_fake.spec.js captures it), or null when nothing was exported.
async function captureExport(page, fn) {
  return page.evaluate(async (src) => {
    const create = URL.createObjectURL, revoke = URL.revokeObjectURL, click = HTMLAnchorElement.prototype.click;
    let blob = null;
    URL.createObjectURL = (b) => { blob = b; return 'blob:hil-export'; };
    URL.revokeObjectURL = () => {};
    HTMLAnchorElement.prototype.click = function () {};
    try { (0, eval)(src)(); } finally {
      URL.createObjectURL = create; URL.revokeObjectURL = revoke; HTMLAnchorElement.prototype.click = click;
    }
    return blob ? blob.text() : null;
  }, `(${fn.toString()})`);
}

const toastsSince = (page, m) => page.evaluate((m) => __fake.toasts.slice(m).map((t) => t.message).join('\n'), m);
const toastMark = (page) => page.evaluate(() => __fake.toasts.length);

// Rename the variable row named `from` on board n, as a user typing into its name cell.
const renameVar = (page, n, from, to) => page.evaluate(({ n, from, to }) => {
  const input = [...document.querySelectorAll(`#b${n}-var-tbody .var-name-input`)].find((i) => i.value === from);
  if (!input) throw new Error(`no variable row ${from} on WCB ${n}`);
  input.value = to;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}, { n, from, to });

test('wizard.push_fake_var_guard a variable row with an invalid name stops the push, the relay push and the export before anything is sent - so the diff cannot clear the variable that row renamed - while a temporary row is never part of the config', async ({ page }) => {
  // getVariablesFromUI (app.js:5469-5485) returns null on a bad name; boardGo (8138-8146), boardGoRemote (8567-8572)
  // and exportSystemFile (9584-9590) stop on it. Row var.push_export_guard.
  await setup(page);
  await pullFake(page, 1, board(['VAR,SET,a,1']));
  await renameVar(page, 1, 'a', 'a b');
  await edit(page, '#b1-s5-label', 'HIL5');
  let m = await toastMark(page);
  let r = await push(page, 1);
  expect(r.sent, 'nothing: not the label, and no ?VAR,CLEAR,a for the renamed row').toEqual([]);
  expect(r.outcome).toEqual({ ok: false, aborted: true, reason: 'invalid variable name — nothing was sent' });
  expect(await toastsSince(page, m)).toContain('WCB 1: invalid variable name "a b"');
  expect(await page.evaluate(() => [document.getElementById('b1-btn-go').disabled, document.getElementById('b1-btn-go').textContent,
                                    _pushingBoards.has(1)])).toEqual([false, 'Push Config', false]);

  // The export stops too: nothing is downloaded.
  m = await toastMark(page);
  expect(await captureExport(page, () => exportSystemFile())).toBe(null);
  expect(await toastsSince(page, m)).toContain('Export aborted — fix the variable name on WCB 1 first');

  // What the guard keeps from the board: the same rename to a valid name sends the clear of the old one (parser.js
  // buildCommandString clears a variable missing from the rows), so a bad row dropped silently would clear 'a'.
  await renameVar(page, 1, 'a b', 'b');
  r = await push(page, 1, { skipReboot: true });
  expect(r.sent).toEqual(expect.arrayContaining(['1:?VAR,CLEAR,a', '1:?VAR,SET,b,1', '1:?LABEL,S5,HIL5']));

  // Through a relay: no session is started for the board at all.
  await openWizard(page);
  await relayFake(page, 1, { 2: chain(board(['VAR,SET,a,1'], { wcb: 2 })) });
  await renameVar(page, 2, 'a', 'a b');
  await edit(page, '#b2-s5-label', 'HIL5');
  r = await page.evaluate(async () => {
    __fake.log.length = 0;
    await boardGo(2);
    return { frags: __fake.sent(1).filter((s) => s.includes('MGMT,FRAG,2,') && !s.includes('RTERM')), outcome: boardPushOutcome[2],
             go: [document.getElementById('b2-btn-go').disabled, document.getElementById('b2-btn-go').textContent] };
  });
  expect(r.frags).toEqual([]);
  expect(r.outcome).toEqual({ ok: false, aborted: true, reason: 'invalid variable name — nothing was sent' });
  expect(r.go).toEqual([false, 'Push Config']);

  // Temporary rows (;V, RAM only) are left out of the config, even one with a name a persistent row may not have:
  // a push sends no ?VAR,SET for them, and the export does not carry them.
  await openWizard(page);
  await pullFake(page, 1, board(['VAR,SET,a,1']));
  await page.evaluate(() => {
    appendTempVariableRow(1, 'tmp', 5, false);
    appendTempVariableRow(1, 'live', 3, true);
    appendTempVariableRow(1, 't x', 4, false);
  });
  expect(await page.evaluate(() => getVariablesFromUI(1))).toEqual([{ name: 'a', value: 1 }]);
  r = await push(page, 1);
  expect([r.sent, r.outcome.reason]).toEqual([[], 'no changes to push']);
  await page.evaluate(() => appendVariableRow(1, 'b', 2));
  r = await push(page, 1, { skipReboot: true });
  expect(r.sent).toEqual(['1:?VAR,SET,b,2']);
  const text = await captureExport(page, () => exportSystemFile());
  const wcb1 = text.split('\n')[text.split('\n').indexOf('[WCB1]') + 1].split('^');
  expect(wcb1).toEqual(expect.arrayContaining(['?VAR,SET,a,1', '?VAR,SET,b,2']));
  expect(wcb1.filter((t) => /VAR,SET,(tmp|live|t x),/.test(t))).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});

// Type into a General field and leave it, as a user does: the input handler, then the blur check.
const typeAndLeave = (page, id, value) => page.evaluate(({ id, value }) => {
  const el = document.getElementById(id);
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('blur'));
}, { id, value });
const typeOnly = (page, id, value) => page.evaluate(({ id, value }) => {
  const el = document.getElementById(id);
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
}, { id, value });

test('wizard.push_fake_mac_validate a MAC octet that is not hex turns its General field red with an error when it is left, a single hex digit is padded to two; a push - direct or through a relay - checks the octets itself, sending nothing for a bad one and the padded value for a single digit; Push All checks them once before any board starts, with one message and nothing pushed anywhere', async ({ page }) => {
  // validateMacOctet (app.js:1324-1340, the General MAC fields' onblur) only marks the field, and boardGo and
  // boardGoRemote read the inputs as they were, so a push sent ?MAC,2,ZZ, which the firmware saves (WCB.md s3 #5); its
  // pad branch was unreachable too (a single digit failed the length test first). Fixed 2026-10-12: the blur pads, and
  // both pushes check the octets through _pushMacOctets (_badMacOctet; boardGo, boardGoRemote). Push All with a bad octet
  // aborted every board, one error toast each; since 2026-10-13 boardGoAll checks once (_badMacOctet) before any board
  // starts. Row general.mac_validate.
  await setup(page);
  await pullFake(page, 1, board());
  const field = (id) => page.evaluate((id) => [document.getElementById(id).value, document.getElementById(id).style.borderColor], id);

  let m = await toastMark(page);
  await typeAndLeave(page, 'g-mac2', 'zz');
  expect(await field('g-mac2')).toEqual(['zz', 'var(--red)']);
  expect(await toastsSince(page, m)).toContain('Invalid hex value "ZZ" — must be 00–FF');
  m = await toastMark(page);
  await typeAndLeave(page, 'g-mac2', '5');
  expect(await field('g-mac2'), 'one digit is padded, and the field is no longer red').toEqual(['05', '']);
  expect(await toastsSince(page, m)).not.toContain('Invalid hex value');
  expect(await page.evaluate(() => [systemConfig.general.macOctet2, boardConfigs[1].macOctet2])).toEqual(['05', '05']);

  // At push time: a bad octet stops the push before anything is sent, whether or not the field was ever left.
  await edit(page, '#b1-s5-label', 'HIL5');
  await typeOnly(page, 'g-mac2', 'ZZ');
  m = await toastMark(page);
  let r = await push(page, 1, { skipReboot: true });
  expect(r.sent).toEqual([]);
  expect(r.outcome).toEqual({ ok: false, aborted: true, reason: 'invalid MAC octet — nothing was sent' });
  expect(await toastsSince(page, m)).toContain('MAC octet 2');
  await typeOnly(page, 'g-mac2', '05');
  await typeOnly(page, 'g-mac3', 'G1');
  expect((await push(page, 1, { skipReboot: true })).sent, 'octet 3 too').toEqual([]);
  // A single digit typed and pushed without leaving the field goes out padded: the board's own 05 is no change.
  await typeOnly(page, 'g-mac3', '4B');
  await typeOnly(page, 'g-mac2', '5');
  expect((await push(page, 1, { skipReboot: true })).sent).toEqual(['1:?LABEL,S5,HIL5']);
  await typeOnly(page, 'g-mac2', 'a');
  r = await push(page, 1, { skipReboot: true });
  expect(r.sent.filter((s) => s.includes('?MAC,'))).toEqual(['1:?MAC,2,0A']);

  // Through a relay: no session, and the network-group confirm never opens for a value that cannot be sent.
  await openWizard(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await typeOnly(page, 'g-mac3', 'XY');
  r = await page.evaluate(async () => {
    __fake.log.length = 0;
    await boardGo(2);
    return { frags: __fake.sent(1).filter((s) => s.includes('MGMT,FRAG,2,') && !s.includes('RTERM')), outcome: boardPushOutcome[2],
             confirm: document.getElementById('network-group-change-modal').classList.contains('open') };
  });
  expect(r).toEqual({ frags: [], outcome: { ok: false, aborted: true, reason: 'invalid MAC octet — nothing was sent' }, confirm: false });

  // Push All - a board on USB, the relay itself and the board through it: the octets are checked once, before any board
  // starts. One message, no per-board toast, nothing sent to any board, and General still pending.
  await typeOnly(page, 'g-mac3', '4B');
  await pullFake(page, 3, board([], { wcb: 3 }));
  await typeOnly(page, 'g-mac2', 'Q7');
  m = await toastMark(page);
  const all = await page.evaluate(async () => {
    __fake.log.length = 0;
    await boardGoAll();
    return { sent: __fake.log.filter((e) => (e.op === 'send' || e.op === 'await') && !e.s.includes('RTERM')).map((e) => `${e.slot}:${e.s}`),
             dirty: generalSettingsDirty };
  });
  expect(all).toEqual({ sent: [], dirty: true });
  expect(await page.evaluate((m) => __fake.toasts.slice(m).map((t) => [t.type, t.message]), m))
    .toEqual([['error', 'MAC octet 2 "Q7" is not a hex value 00–FF — fix it in General first. Nothing was pushed to any board.']]);
  expect(page.wizErrors).toEqual([]);
});

// ── The flash and erase paths ────────────────────────────────────────────────────────────────────────────────────

// Give the fake in `slot` what the flash and erase paths need: a port object (boardGo hands it to flashFirmware and
// closes it after a flash, both recorded), a reader (waitForBoardReady probes only a connection that reads), and a
// board that answers the probe with the backup's end marker while `ready` is set.
async function answering(page, slot, { ready = true } = {}) {
  await page.evaluate(({ slot, ready }) => {
    const c = boardConnections[slot];
    const rec = (op, s = '') => __fake.log.push({ slot: c.boardIndex, op, s, at: Date.now() });
    c.port = { hil: `port${slot}`, close: async () => rec('portclose'), getInfo: () => ({ usbVendorId: 0x1a86, usbProductId: 0x55d4 }) };
    c.reader = { hil: true };
    c.__ready = ready;
    const send = c.send;
    c.send = async (s) => {
      await send(s);
      if (c.__ready && /^WCB_WEBTOOL_CONFIG_PULL/.test(String(s))) setTimeout(() => c._handleLine('--------- End of Backup ---------'), 20);
    };
  }, { slot, ready });
}

// window.flashFirmware as a recorder: its arguments, and what the card showed while it ran. `drift` is a pull landing
// during the flash: the board's label and the General MAC octet 3 change under it. `fail` makes it throw.
async function stubFlash(page, slot, { fail = false, drift = false } = {}) {
  await page.evaluate(({ slot, fail, drift }) => {
    window.__flashCalls = [];
    window.flashFirmware = async (port, hw, opts) => {
      const q = (id) => document.getElementById(id);
      const c = boardConnections[slot];
      __fake.log.push({ slot, op: 'flash', s: '', at: Date.now() });
      opts.onProgress(50, 100);
      opts.onStatus('Writing');
      window.__flashCalls.push({
        port: port?.hil ?? null, hw, appOnly: opts.appOnly, eraseNvs: opts.eraseNvs, connected: c._connected,
        connect: q(`b${slot}-btn-connect`).textContent, connectFlashing: q(`b${slot}-btn-connect`).classList.contains('btn-flashing'),
        go: q(`b${slot}-btn-go`).textContent, goDisabled: q(`b${slot}-btn-go`).disabled,
        wrap: q(`b${slot}-flash-wrap`).style.display, bar: q(`b${slot}-flash-bar`).style.width,
        status: q(`b${slot}-flash-status`).textContent.split(' • ')[0], flashing: !!_boardFlashing[slot], pushing: _pushingBoards.has(slot),
      });
      if (drift) {
        boardConfigs[slot].serialPorts[0].label = 'Drifted';
        q('g-mac3').value = '7E';
      }
      if (fail) throw new Error('Bootloader connection failed (hil)');
    };
  }, { slot, fail, drift });
}

// Every boardGo call from here on - the one the test makes and the ones the flash paths make - with its mode and
// whether the board's baseline was null (a full push) when it was called.
async function recordGo(page) {
  await page.evaluate(() => {
    if (window.__realBoardGo) return;
    window.__realBoardGo = boardGo;
    window.__goCalls = [];
    window.boardGo = (n, opts = {}) => {
      __goCalls.push({ n, mode: opts.mode ?? null, baselineNull: boardBaselines[n] === null, at: Date.now() });
      return window.__realBoardGo(n, opts);
    };
  });
}

// The log since the last reset, as { op, s, at } for slot 1.
const log1 = (page) => page.evaluate(() => __fake.log.filter((e) => e.slot === 1).map((e) => ({ op: e.op, s: e.s, at: e.at })));
const first = (log, op, s) => log.find((e) => e.op === op && (s === undefined || (s instanceof RegExp ? s.test(e.s) : e.s === s)));

test('wizard.flash_fake_entry_snapshot Flash, Update and Factory close the port before flashing, show Flashing... on the card while the flasher runs, hand flashFirmware the board\'s port and HW version with appOnly for Update and eraseNvs for Factory, wire its progress into the card, and put the card back afterwards - after a flash that worked and after one that failed', async ({ page }) => {
  // boardGo's flash entry, app.js:7751-7815 (and the failed flash's reconnect, 7935-8013). The pre-flash snapshot it
  // takes there is what the post-flash push sends: wizard.flash_fake_post_success. Row flash.entry_snapshot.
  await setup(page);
  const run = async (mode, { fail = false } = {}) => {
    await openWizard(page);
    await pullFake(page, 1, board());
    await answering(page, 1, { ready: false });
    await stubFlash(page, 1, { fail });
    await page.evaluate(() => {
      updateConnectionUI(1, true);                 // as a real connect leaves the card
      const c = boardConnections[1];
      const reconnect = c.reconnect;
      c.reconnect = async (...a) => { __fake.log.push({ slot: 1, op: 'reconnectArgs', s: JSON.stringify(a), at: Date.now() }); return reconnect(...a); };
      __fake.log.length = 0;
    });
    const m = await toastMark(page);
    await page.evaluate((mode) => boardGo(1, { mode }), mode);
    const q = await page.evaluate(() => {
      const q = (id) => document.getElementById(id);
      return { calls: __flashCalls, connect: q('b1-btn-connect').textContent, connectFlashing: q('b1-btn-connect').classList.contains('btn-flashing'),
               go: q('b1-btn-go').textContent, goDisabled: q('b1-btn-go').disabled, wrap: q('b1-flash-wrap').style.display,
               flashing: !!_boardFlashing[1], pushing: _pushingBoards.has(1), mode: boardFlashMode[1] ?? null };
    });
    return { ...q, log: await log1(page), toasts: await toastsSince(page, m) };
  };
  const DURING = { port: 'port1', hw: 24, connected: false, connect: 'Flashing…', connectFlashing: true, go: 'Flashing…', goDisabled: true,
                   wrap: '', bar: '50%', status: 'Writing', flashing: true, pushing: true };
  const AFTER = { connect: 'Disconnect', connectFlashing: false, go: 'Push Config', goDisabled: false, wrap: 'none', flashing: false, pushing: false };

  for (const [mode, appOnly, eraseNvs, label] of [['update', true, false, 'updated'], ['flash', false, false, 'flashed'],
                                                  ['factory', false, true, 'factory reset']]) {
    const r = await run(mode);
    expect.soft(r.calls, mode).toEqual([{ ...DURING, appOnly, eraseNvs }]);
    const ops = r.log.map((e) => e.op);
    expect.soft(ops.slice(0, 2), `${mode}: the port closed before the flasher ran`).toEqual(['close', 'flash']);
    // A flash that worked: the port closed once more after a 1.5 s pause, then reopened (12 x 2 s off Windows).
    const flash = first(r.log, 'flash'), pclose = first(r.log, 'portclose');
    expect.soft(pclose && pclose.at - flash.at, `${mode}: the post-flash close`).toBeGreaterThanOrEqual(1450);
    expect.soft(first(r.log, 'reconnectArgs')?.s, mode).toBe('[12,2000]');
    expect.soft({ connect: r.connect, connectFlashing: r.connectFlashing, go: r.go, goDisabled: r.goDisabled, wrap: r.wrap,
                  flashing: r.flashing, pushing: r.pushing }, `${mode}: the card afterwards`).toEqual(AFTER);
    expect.soft(r.mode, `${mode}: the next push is a plain one`).toBe('configure');
    expect.soft(r.toasts, mode).toContain(`WCB 1 firmware ${label} in`);
  }

  // A flash that failed: the error said, the same port reopened (no search for another board's), the card put back.
  const r = await run('flash', { fail: true });
  expect(r.toasts).toContain('Flash failed: Bootloader connection failed (hil)');
  expect(r.log.map((e) => e.op).slice(0, 3)).toEqual(['close', 'flash', 'reconnectArgs']);
  expect(first(r.log, 'reconnectArgs').s).toBe('[5,1500,{"skipFreshPortSearch":true}]');
  expect(first(r.log, 'portclose'), 'off Windows nothing is closed before the reopen').toBeUndefined();
  expect({ connect: r.connect, connectFlashing: r.connectFlashing, go: r.go, goDisabled: r.goDisabled, wrap: r.wrap,
           flashing: r.flashing, pushing: r.pushing }).toEqual(AFTER);
  // A factory reset that failed says the board was not reset.
  const f = await run('factory', { fail: true });
  expect(f.toasts).toContain('WCB 1: factory reset incomplete — retry required');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flash_fake_post_success after a flash the Wizard reopens the port and waits for the firmware: Flash pushes the whole pre-flash config with the General fields as they were before it (whatever a pull changed meanwhile), an Update only pulls (or, in the setup wizard, pushes), Factory with push unchecked only pulls, a port borrowed from the shared hub is given back, and a board that never comes back is said to', async ({ page }) => {
  // boardGo after a flash that worked, app.js:7820-7935; waitForBoardReady 7314-7381. Row flash.post_success.
  await setup(page);
  const start = async ({ ready = true, reconnects = true, wizard = false, borrowed = false, drift = false } = {}) => {
    await openWizard(page);
    await pullFake(page, 1, board(['LABEL,S1,Keep']));
    await answering(page, 1, { ready });
    await stubFlash(page, 1, { drift });
    await recordGo(page);
    await page.evaluate(({ reconnects, wizard, borrowed }) => {
      const c = boardConnections[1];
      updateConnectionUI(1, true);
      if (!reconnects) c.reconnect = async () => { __fake.log.push({ slot: 1, op: 'reconnect', s: 'false', at: Date.now() }); c._connected = false; return false; };
      if (borrowed) {
        // A port boardGo borrowed from the shared hub for the flash (app.js:7739-7744): re-shared once it is back.
        c._wasSharedBeforeFlash = true;
        c.becomeShared = async () => { __fake.log.push({ slot: 1, op: 'reshare', s: '', at: Date.now() }); };
      }
      _wizardOpen = wizard;
      __fake.log.length = 0;
      __goCalls.length = 0;
    }, { reconnects, wizard, borrowed });
  };
  const pushed = (log) => log.filter((e) => e.op === 'await').map((e) => e.s);
  const waitFor = (fn, timeout = 20_000) => page.waitForFunction(fn, null, { timeout });

  // Flash: the probe after the reconnect, then the whole snapshot pushed from a null baseline, General restored.
  await start({ drift: true });
  await page.evaluate(() => boardGo(1, { mode: 'flash' }));
  await waitFor(() => __fake.log.some((e) => e.s === '?reboot'));
  let log = await log1(page);
  const reconnect = first(log, 'reconnect'), probe = first(log, 'send', 'WCB_WEBTOOL_CONFIG_PULL'), firstPush = first(log, 'await');
  expect(reconnect && probe && firstPush, JSON.stringify(log.map((e) => e.op))).toBeTruthy();
  expect(probe.at - reconnect.at, 'waitForBoardReady waits 5 s before its first probe').toBeGreaterThanOrEqual(4950);
  expect(firstPush.at).toBeGreaterThan(probe.at);
  const goCalls = await page.evaluate(() => __goCalls.map(({ mode, baselineNull }) => ({ mode, baselineNull })));
  expect(goCalls).toEqual([{ mode: 'flash', baselineNull: false }, { mode: 'configure', baselineNull: true }]);
  const flashPush = pushed(log);
  // A full push: settings the board already had (a diff would send none of them), its label as it was before the flash
  // - not the one a pull wrote during it - and General's MAC octet as it was then, not as it is now.
  expect(flashPush).toEqual(expect.arrayContaining(['?WCB,1', '?WCBQ,2', '?HW,24', '?LABEL,S1,Keep', '?MAC,3,4B']));
  expect(flashPush.filter((s) => s.includes('Drifted') || s === '?MAC,3,7E')).toEqual([]);
  expect(await page.evaluate(() => document.getElementById('g-mac3').value), 'General put back').toBe('4B');

  // Update outside the setup wizard: NVS is intact, so the board is only pulled once it answers - nothing pushed.
  await start({ borrowed: true });
  await page.evaluate(() => boardGo(1, { mode: 'update' }));
  await waitFor(() => __fake.log.some((e) => e.op === 'collect'));
  log = await log1(page);
  // (The pull's ?version collect, fetchBoardVersion, is left out.)
  expect(log.filter((e) => ['flash', 'reconnect', 'reshare'].includes(e.op) || e.s === 'WCB_WEBTOOL_CONFIG_PULL').map((e) => e.op))
    .toEqual(['flash', 'reconnect', 'reshare', 'send', 'collect']);
  expect(pushed(log)).toEqual([]);
  expect(await page.evaluate(() => [__goCalls.length, boardConnections[1]._wasSharedBeforeFlash])).toEqual([1, false]);

  // Update inside the setup wizard: awaited, and the wizard's config (the pre-flash snapshot) pushed whole.
  await start({ wizard: true });
  await page.evaluate(() => boardGo(1, { mode: 'update' }));     // returns only once the push has run
  log = await log1(page);
  expect(await page.evaluate(() => __goCalls.map(({ mode, baselineNull }) => ({ mode, baselineNull }))))
    .toEqual([{ mode: 'update', baselineNull: false }, { mode: 'configure', baselineNull: true }]);
  expect(pushed(log)).toEqual(expect.arrayContaining(['?WCB,1', '?LABEL,S1,Keep']));

  // Factory with "push config" unchecked: the fresh defaults are pulled, nothing pushed.
  await start();
  await page.evaluate(() => boardGo(1, { mode: 'factory', pushConfig: false }));
  await waitFor(() => __fake.log.some((e) => e.op === 'collect'));
  log = await log1(page);
  expect(pushed(log)).toEqual([]);
  expect(first(log, 'collect').at).toBeGreaterThan(first(log, 'send', 'WCB_WEBTOOL_CONFIG_PULL').at);
  expect(await page.evaluate(() => [__flashCalls[0].eraseNvs, __goCalls.length])).toEqual([true, 1]);

  // The board never comes back: said, no probe, nothing pushed, and the card's Push button usable again.
  await start({ reconnects: false });
  const m = await toastMark(page);
  await page.evaluate(() => boardGo(1, { mode: 'flash' }));
  await page.waitForTimeout(6000);
  log = await log1(page);
  expect(await toastsSince(page, m)).toContain('WCB 1 did not come back — reconnect manually');
  expect(log.filter((e) => e.op === 'send' || e.op === 'await')).toEqual([]);
  expect(await page.evaluate(() => [document.getElementById('b1-btn-go').textContent, document.getElementById('b1-btn-go').disabled]))
    .toEqual(['Push Config', false]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flash_fake_erase_path the setup wizard\'s erase sends ?ERASE,NVS, keeps the port open 4 s, closes and reopens it, waits for the firmware and pushes the whole pre-erase config with its General fields; the Factory Reset modal\'s Erase NVS Only does the same up to the reopen, then only pulls', async ({ page }) => {
  // boardGo's erase path, app.js:8015-8114 (both branches: the setup wizard open, which awaits the push, and closed -
  // erase is only ever the wizard's mode, so the second runs when it was closed during the erase); doFactoryReset-
  // EraseOnly, 10306-10350. The closed branch did not put General back (the open one and the flash path do); fixed
  // 2026-10-12. Row flash.erase_path.
  await setup(page);
  const start = async ({ wizard }) => {
    await openWizard(page);
    await pullFake(page, 1, board(['LABEL,S1,Keep']));
    await answering(page, 1);
    await recordGo(page);
    await page.evaluate((wizard) => {
      updateConnectionUI(1, true);
      _wizardOpen = wizard;
      __fake.log.length = 0;
      __goCalls.length = 0;
      // A pull landing while the board erases: its label and General's MAC octet 3 change under the Wizard.
      setTimeout(() => { boardConfigs[1].serialPorts[0].label = 'Drifted'; document.getElementById('g-mac3').value = '7E'; }, 1000);
    }, wizard);
  };
  const check = async (what) => {
    const log = await log1(page);
    const erase = first(log, 'send', '?ERASE,NVS'), close = first(log, 'close'), reconnect = first(log, 'reconnect');
    const probe = first(log, 'send', 'WCB_WEBTOOL_CONFIG_PULL'), firstPush = first(log, 'await');
    expect(erase && close && reconnect && probe && firstPush, `${what}: ${JSON.stringify(log.map((e) => e.op + ' ' + e.s.slice(0, 20)))}`).toBeTruthy();
    expect(close.at - erase.at, `${what}: the port stays open while the board erases`).toBeGreaterThanOrEqual(3950);
    expect(reconnect.at).toBeGreaterThanOrEqual(close.at);
    expect(probe.at - reconnect.at, `${what}: the firmware is waited for`).toBeGreaterThanOrEqual(4950);
    expect(firstPush.at).toBeGreaterThan(probe.at);
    const pushedNow = log.filter((e) => e.op === 'await').map((e) => e.s);
    expect(pushedNow, `${what}: the whole pre-erase config`).toEqual(expect.arrayContaining(['?WCB,1', '?WCBQ,2', '?HW,24', '?LABEL,S1,Keep', '?MAC,3,4B']));
    expect(pushedNow.filter((s) => s.includes('Drifted') || s === '?MAC,3,7E'), what).toEqual([]);
    expect(await page.evaluate(() => __goCalls.map(({ mode, baselineNull }) => ({ mode, baselineNull }))), what)
      .toEqual([{ mode: 'erase', baselineNull: false }, { mode: 'configure', baselineNull: true }]);
  };

  // The setup wizard open: boardGo returns once the push has run.
  await start({ wizard: true });
  await page.evaluate(() => boardGo(1, { mode: 'erase' }));
  await check('wizard open');

  // Closed meanwhile: the push follows in the background, with the same snapshot.
  await start({ wizard: false });
  await page.evaluate(() => boardGo(1, { mode: 'erase' }));
  await page.waitForFunction(() => __fake.log.some((e) => e.op === 'await'), null, { timeout: 20_000 });
  await page.waitForTimeout(500);
  await check('wizard closed');

  // Factory Reset > Erase NVS Only: the same erase and reopen, then a pull 3 s later and no push.
  await openWizard(page);
  await pullFake(page, 1, board());
  await answering(page, 1);
  await recordGo(page);
  await page.evaluate(() => { updateConnectionUI(1, true); __fake.log.length = 0; __goCalls.length = 0; });
  let m = await toastMark(page);
  await page.evaluate(() => { _factoryResetSlot = 1; return doFactoryResetEraseOnly(); });
  await page.waitForFunction(() => __fake.log.some((e) => e.op === 'collect'), null, { timeout: 15_000 });
  const log = await log1(page);
  const erase = first(log, 'send', '?ERASE,NVS'), close = first(log, 'close'), reconnect = first(log, 'reconnect'), pull = first(log, 'collect');
  expect(close.at - erase.at).toBeGreaterThanOrEqual(3950);
  expect(pull.at - reconnect.at, 'pulled 3 s after the reopen').toBeGreaterThanOrEqual(2950);
  expect(pull.s).toBe('WCB_WEBTOOL_CONFIG_PULL');
  expect(log.filter((e) => e.op === 'await'), 'nothing pushed').toEqual([]);
  expect(await page.evaluate(() => [__goCalls.length, _factoryResetSlot])).toEqual([0, null]);
  expect(await toastsSince(page, m)).toContain('WCB 1 reconnected');
  // A board that is not connected: refused, nothing sent.
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  m = await toastMark(page);
  await page.evaluate(() => { _factoryResetSlot = 1; return doFactoryResetEraseOnly(); });
  expect(await toastsSince(page, m)).toContain('Board not connected');
  expect(await log1(page)).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});
