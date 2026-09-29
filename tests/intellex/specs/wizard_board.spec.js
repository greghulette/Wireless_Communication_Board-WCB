// The WCB Wizard through a staged Intellex host attached to W1's COM port (INTELLEX.md IX-WP7), and the shell with both
// tools on that one link. Each test is started by the harness test of the same id (tests/hil/suites/
// s34_intellex_tools.py), which releases W1's port to the host, keeps every other board and probe, owns the before/after
// config check (config_guard), stops W2's remote terminal afterwards (the shim's mesh routing arms it) and checks from a
// raw /_link client of its own that W1 printed no boot line. The Wizard connects on its own: Intellex's shim replaces
// the port picker (intellex_shim.js autoConnectWcb -> _modalDoConnect(1, port)), so there is no Chrome profile and no
// port grant; these are ports of tests/wizard/specs/board.spec.js and remote_pull.spec.js to that auto-connect.
//
// The harness hands a spec lengths, versions, keys, labels and bauds, never a token that carries a secret: W1's ?backup
// and every pull carry the mesh password and the WiFi passphrase. Nothing here quotes a line the board printed.
const { test, expect, skipUnlessHost } = require('../lib/fixtures');
const B = require('../lib/board');

skipUnlessHost(test);
test.beforeEach(() => {
  test.skip(!B.hil.present, 'board tests run under the HIL harness: python tests/hil/run.py "intellex.wizard_*"');
});

const args = async () => (await B.hil.context()).args || {};
const pathOf = (f) => { try { return new URL(f.url()).pathname; } catch (_) { return ''; } };

// Wait until the shim has pulled every other board the harness knows on the mesh through W1 (routeMeshThroughBoard),
// or `ms` passed: a push or a terminal line sent meanwhile would share W1's stream with a pull. -> the boards not
// pulled in time (reported by the caller, not failed: it is not what these tests check).
async function meshSettled(page, boards, ms = 60_000) {
  const until = Date.now() + ms;
  for (;;) {
    const left = await page.evaluate((bs) => bs.filter((b) => !boardBaselines[b] || _pullingBoards.has(b)), boards);
    if (!left.length || Date.now() > until) return left;
    await page.waitForTimeout(1000);
  }
}

test('intellex.wizard_pull_w1 the Wizard pulls W1 through Intellex with no click and shows its bauds and labels',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  const g = await B.boardGuard(context, rec);
  const t0 = Date.now();
  await B.openWizard(page);
  const n = a.wcb;
  expect(await B.waitPulled(page, n), `the board pulled into slot ${n} with no click`).toBe(n);
  const took = Date.now() - t0;
  let checked = 0;
  for (const t of a.tokens) {
    let m = t.match(/^\?BAUD,S([1-5]),(\d+)$/i);
    if (m) {
      await expect(page.locator(`#b${n}-s${m[1]}-baud`), t).toHaveValue(m[2]);
      checked++;
    }
    m = t.match(/^\?LABEL,S([1-5]),(.*)$/i);
    if (m) {
      await expect(page.locator(`#b${n}-s${m[1]}-label`), t).toHaveValue(m[2]);
      checked++;
    }
  }
  expect(checked, 'the harness sent no ?BAUD / ?LABEL tokens to compare').toBeGreaterThan(0);
  // intellex_shim.js wcbChip / annotateWcb, on its 2 s tick: what the link is, outside the shell.
  await expect.poll(() => page.evaluate(() => (document.getElementById('intellex-chip') || {}).textContent || ''),
    { timeout: 6_000, message: 'the Wizard chip' }).toBe(`Intellex · USB ${a.com} ▾`);
  expect(await B.liveSlots(page), 'the slots with a live connection').toEqual([n]);
  const sig = g.signals.map((s) => `${JSON.stringify(s.body)}->${s.status}`).join(', ') || 'none';
  await B.hil.note(`intellex.wizard_pull_w1: W${n} pulled into slot ${n} ${took} ms after the page was opened, with ` +
    `no click; the page's setSignals calls: ${sig}`);
  B.expectClean(rec, 'the Wizard');
});

test('intellex.wizard_push_label_w1 a label typed into the Wizard and pushed through Intellex',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  await B.openWizard(page);
  const n = a.wcb;
  expect(await B.waitPulled(page, n)).toBe(n);
  const left = await meshSettled(page, a.boards);
  if (left.length) {
    await B.hil.note(`intellex.wizard_push_label_w1: WCB${left.join(', ')} not pulled through W${n} yet; pushing anyway`);
  }
  await B.setField(page, `#b${n}-s${a.port}-label`, a.label);
  const outcome = await B.pushConfig(page, n);
  expect(outcome, JSON.stringify(outcome)).toMatchObject({ ok: true, aborted: false });
  B.expectClean(rec, 'the Wizard');
  // The harness reads W1's ?backup once the host lets go of the port, and puts the label back.
});

test('intellex.wizard_terminal_wire a command typed in the Wizard terminal through Intellex reaches the probe once',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  await B.openWizard(page);
  const n = a.wcb;
  expect(await B.waitPulled(page, n)).toBe(n);
  await meshSettled(page, a.boards);
  const text = `IXW${Date.now().toString(36).toUpperCase()}`;
  const w = B.hil.wire(n, a.port);
  const since = await w.mark();
  // The Wizard ends a terminal line with CR; the shim frames per line and keeps it (intellex_shim.js:202-212).
  await page.evaluate(({ n, cmd }) => {
    document.getElementById(`term-pane-input-${n}`).value = cmd;
    return sendTerminalCommandTo(n);
  }, { n, cmd: `;${a.port}${text}` });
  await w.expect(`${text}\r`, since, 4);
  await page.waitForTimeout(1500);
  const got = (await w.received(since)).toString('latin1');
  expect(got.split(`${text}\r`).length - 1, `${a.port} carried the line`).toBe(1);
  B.expectClean(rec, 'the Wizard');
});

// The mesh as the shim routes it through a plain WCB: every other WCB W1 hears is armed (setRemoteConnected) and pulled
// once, sequentially, and clients - NaviCore at 20, a probe joined as a client - never (routeMeshThroughBoard,
// intellex_shim.js:1197-1316). a.boards: the WCBs the harness knows besides W1; a.clients: the clients it expects W1 to
// hear.
async function meshRouting(page, rec, a) {
  await B.openWizard(page);
  await B.spyRemotePulls(page);
  expect(await B.waitPulled(page, a.relay)).toBe(a.relay);
  const pulled = () => page.evaluate((bs) => bs.every((b) => !!boardBaselines[b] && !_pullingBoards.has(b)), a.boards);
  await expect.poll(pulled, { timeout: 120_000,
    message: `the shim did not pull WCB${a.boards.join(', ')} through W${a.relay}` }).toBe(true);
  // Two more of the Wizard's 12 s mesh sweeps (meshAutoDiscoverTick): nothing is pulled again.
  await page.waitForTimeout(26_000);
  const res = {};
  for (const b of a.boards.concat(a.clients)) res[b] = await B.pullResult(page, b);
  const calls = res[a.boards[0]].calls;
  const shim = (b) => calls.filter((c) => c.target === b && c.byShim);
  for (const b of a.boards) {
    const r = res[b];
    expect(r.baseline && r.wcbNumber, `WCB${b}: a baseline naming its own number`).toBe(b);
    expect(String(r.relayFor), `WCB${b} is managed through W${a.relay}`).toBe(String(a.relay));
    expect(shim(b).length, `the shim pulled WCB${b} once`).toBe(1);
    expect(r.done.filter((d) => d.target === b && d.byShim).map((d) => d.ok),
      `WCB${b}: the shim's pull completed once, with true`).toEqual([true]);
    expect(r.stillPulling, `WCB${b} left _pullingBoards`).toBe(false);
  }
  const stray = calls.filter((c) => !a.boards.includes(c.target)).map((c) => c.target);
  expect(stray, 'pulls of anything but the bench\'s WCBs (a client, a probe)').toEqual([]);
  for (const c of a.clients) {
    expect(res[c].client || res[c].type === 'client', `WCB${c} is shown as a client`).toBe(true);
    expect(res[c].baseline, `client ${c} has no config baseline`).toBe(false);
  }
  const own = calls.filter((c) => !c.byShim).map((c) => c.target);
  const each = a.boards.map((b) => `WCB${b} pulled through W${a.relay} in ` +
    `${res[b].done.find((d) => d.byShim)?.ms} ms, relay slot passed as ${res[b].calls.find((c) => c.byShim)?.relayType} ` +
    `and filed as ${res[b].relayForType}`);
  await B.hil.note(`${test.info().title.split(' ')[0]}: ${each.join('; ')}` +
    (own.length ? `; the Wizard's own pulls: WCB${own.join(', ')}` : ''));
  B.expectClean(rec, 'the Wizard');
  return res;
}

test('intellex.wizard_mesh_autopull_w2 the shim pulls every WCB it hears through W1 once, and never a client',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  await meshRouting(page, rec, a);
});

// (should) INTELLEX.md finding 17: routeMeshThroughBoard passes the relay slot as _wdpMeshConn() gives it, the string key
// of Object.entries(boardConnections) (Wizard/app.js:13420-13428), to setRemoteConnected and remoteBoardPull
// (intellex_shim.js:1264, :1279). The Wizard's own entry points pass a number (app.js:538 relayManageOne, :6364
// modalRemoteConnect; :2022 re-passes what is stored), and it compares relay slots with === (app.js:5790 the [TERM:n]
// demux, :5939-5942 the boards re-armed after a relay drop, :6432-6436 clearRemoteBoardsForRelay): a board the shim
// filed under '1' is neither cleared nor re-armed when W1's link drops.
test('intellex.wizard_mesh_relay_slot_number a board the shim routes through W1 is filed under relay slot 1, a number',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  await B.openWizard(page);
  await B.spyRemotePulls(page);
  expect(await B.waitPulled(page, a.relay)).toBe(a.relay);
  const b = a.boards[0];
  await expect.poll(() => page.evaluate((b) => remoteRelayForBoard[b] !== undefined, b),
    { timeout: 90_000, message: `the shim never armed WCB${b} through W${a.relay}` }).toBe(true);
  const r = await B.pullResult(page, b);
  expect(r.relayFor, `(should) remoteRelayForBoard[${b}] is ${JSON.stringify(r.relayFor)} (a ${r.relayForType}): the ` +
    `Wizard compares relay slots with === against numbers, so a board filed under the string misses the relay-drop ` +
    `clear and re-arm (INTELLEX.md finding 17)`).toBe(a.relay);
});

test('intellex.wizard_remote_pull_parts the shim pulls W2 through W1 in parts and the Wizard joins them',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  await B.openWizard(page);
  await B.spyRemotePulls(page);
  expect(await B.waitPulled(page, a.relay)).toBe(a.relay);
  // The shim's own pull of the target (no click, no call from here); then 3 s more, so a second onComplete would show.
  await expect.poll(() => page.evaluate((t) => window.__hilPulls.done.some((d) => d.target === t && d.byShim), a.target),
    { timeout: 120_000, message: `the shim never finished a pull of WCB${a.target} through W${a.relay}` }).toBe(true);
  await page.waitForTimeout(3000);
  const res = await B.pullResult(page, a.target);
  const mine = res.seen.filter((s) => s.n === a.target);
  expect(res.done.filter((d) => d.target === a.target && d.byShim).map((d) => d.ok),
    "the shim's pull: onComplete exactly once, with true").toEqual([true]);
  expect(res.sent.length, 'the Wizard sent a pull').toBeGreaterThan(0);
  for (const s of res.sent) expect(s, 'the Wizard asks for parts').toMatch(new RegExp(`^.MGMT,PULL,${a.target},P$`));
  expect(mine.filter((s) => s.tag === 'CFGERR' && s.code !== 'NOPARTS').map((s) => s.code),
    'no CFGERR but a NOPARTS the Wizard asked again after').toEqual([]);
  expect(res.wcbNumber, 'the pulled config names its board').toBe(a.target);
  expect(res.fwVersion, 'the version from [VER:]').toBe(a.ver);
  expect(res.seqs, 'every throwaway sequence arrived whole').toEqual(a.seqs);
  expect(res.baseline, 'a baseline was stored').toBe(true);
  expect(res.stillPulling, 'the board left _pullingBoards').toBe(false);
  expect(res.rawInTerminal, 'the terminal shows no raw pull text').toBe(false);
  const parts = mine.filter((s) => s.tag === 'CFGPART');
  expect(parts.every((s) => s.id && s.framed), 'every part line is P<id>,<k>,<K>:<data>~').toBe(true);
  // The id whose parts all arrived after the last NOPARTS (remote_pull.spec.js): a retry uses a new id.
  const noParts = mine.filter((s) => s.tag === 'CFGERR' && s.code === 'NOPARTS').length;
  const after = mine.slice(mine.map((s) => s.tag === 'CFGERR' && s.code === 'NOPARTS').lastIndexOf(true) + 1);
  const byId = new Map();
  for (const s of after.filter((x) => x.tag === 'CFGPART')) {
    const e = byId.get(s.id) || { K: s.K, lens: new Map() };
    if (!e.lens.has(s.k)) e.lens.set(s.k, s.dataLen);
    byId.set(s.id, e);
  }
  const whole = [...byId.values()].filter((e) => e.lens.size === e.K);
  expect(whole.length, `a whole set of parts (ids seen: ${[...byId.keys()].join(', ') || 'none'})`).toBeGreaterThan(0);
  const e = whole[whole.length - 1];
  expect(e.K, 'parts in the reply').toBeGreaterThanOrEqual(a.minParts);
  expect([...e.lens.values()].reduce((x, y) => x + y, 0), 'the parts add up to the reply').toBe(a.length);
  expect(mine.filter((s) => s.tag === 'CONFIG' && s.len > 0).length, 'no [MGMT:CONFIG] line with a body').toBe(0);
  await B.hil.note(`intellex.wizard_remote_pull_parts: ${e.K} parts of ${[...e.lens.values()].join('+')} characters ` +
    `under ${byId.size} id(s) crossed the Intellex link, settled in ${res.done.find((d) => d.byShim).ms} ms` +
    (noParts ? `, after ${noParts} NOPARTS asked again` : ''));
  B.expectClean(rec, 'the Wizard');
});

test('intellex.wizard_reload_no_reset_w1 three reloads of the Wizard through Intellex, back in slot 1 each time',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await B.openWizard(page);
  const n = a.wcb;
  expect(await B.waitPulled(page, n)).toBe(n);
  for (let i = 1; i <= 3; i++) {
    await B.pressF5(page);
    await page.waitForFunction(() => typeof boardBaselines !== 'undefined' && typeof _boardPullInFlight !== 'undefined');
    expect(await B.waitPulled(page, n), `after reload ${i}: W${n} pulled again into slot ${n}`).toBe(n);
    expect(await B.liveSlots(page), `after reload ${i}: the slots with a live connection`).toEqual([n]);
  }
  expect(link.sockets, 'a /_link per page load').toBeGreaterThanOrEqual(4);
  B.expectClean(rec, 'the Wizard');
  // The harness's own /_link client holds every byte W1 printed across the reloads: no boot line (boot_check).
});

// Both tools in the shell's split view, preloaded and connected over the one link to W1 (Intellex src/shell.html). The
// NaviCore pane's handshake through W1 is finding 4's territory: its transport is recorded here, and judged by
// intellex.nc_via_usb_doorway.
test('intellex.shell_split_w1 both tools connect through the one link to W1 in the split shell',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await page.setViewportSize({ width: 1406, height: 900 });
  const a = await args();
  await B.boardGuard(context, rec);
  const link = B.watchLink(page);
  await page.goto('/_shell?view=split', { waitUntil: 'load' });
  const frame = async (path) => {
    await expect.poll(() => page.frames().some((f) => pathOf(f) === path), { timeout: 15_000,
      message: `no ${path} frame in the split shell` }).toBe(true);
    return page.frames().find((f) => pathOf(f) === path);
  };
  const wiz = await frame('/wcb/Wizard/');
  const nc = await frame('/');
  await wiz.waitForFunction(() => typeof boardBaselines !== 'undefined' && typeof _boardPullInFlight !== 'undefined');
  expect(await B.waitPulled(wiz, a.wcb, 60_000), 'the Wizard pane pulled W1').toBe(a.wcb);
  expect(await wiz.evaluate(() => !document.getElementById('intellex-chip')), 'no Wizard chip inside the shell ' +
    '(the shell shows the link)').toBe(true);
  await expect.poll(() => page.evaluate(() => document.getElementById('target').textContent),
    { timeout: 6_000, message: "the shell's link label" }).toBe(`USB ${a.com} ▾`);
  // The end of the pane's handshake (START_MONITOR sent), not its CONFIG: over W1 that crosses the mesh in fragment
  // envelopes paced 150 ms apart, and reaches W1's USB only inside its relay window (finding 4's territory, judged by
  // intellex.nc_via_usb_doorway).
  const s = await B.ncHandshake(nc, { config: false, timeout: 60_000 });
  if (a.navicore) {
    expect(s.status, 'the NaviCore pane: NaviCore answered through W1').toMatch(/^Connected/);
    await expect.poll(async () => (await B.ncState(nc)).transport, { timeout: 6_000,
      message: "the NaviCore pane's link label" }).toBe(`· USB ${a.com} ▾`);
  } else {
    expect(s.status, 'the NaviCore pane with no NaviCore on the mesh').toBe('Waiting for board…');
  }
  const writes = B.unaskedWrites(link);
  expect(writes, 'JSON the NaviCore pane sent through W1 that is not a read').toEqual([]);
  const json = link.sent.filter((x) => x.type);
  await B.hil.note(`intellex.shell_split_w1: NaviCore pane '${s.status}', viaWcbActive ${s.viaWcbActive}, PONG phase ` +
    `${s.pongEpoch}; ${json.length} JSON lines sent (${json.filter((x) => x.wrapped).length} as ;w20,)`);
  B.expectClean(rec, 'the split shell');
});

// (should) INTELLEX.md finding 16. The Wizard's connect asserts DTR (Wizard/app.js:5364, setSignals({dataTerminalReady:
// true}); RTS left alone), and through Intellex that reaches W1's CH9102 (intellex_shim.js:248-260 setSignals -> host.py
// :1239-1260 api_signals -> serial_transport.py set_signals), whose auto-reset circuit drives GPIO0 from DTR
// (docs/HIL_TESTING.md §2). Chrome's own Web Serial open asserts both lines, which leaves GPIO0 high; Intellex opens with
// both low (serial_transport.py:65-68), so the Wizard's DTR alone holds GPIO0 low for the session. A restart then boots
// the ESP32 by its strapping pins: the app, or the ROM loader ('waiting for download'), where W1 stays until an EN
// reset. W1 is restarted once here with ?reboot from the Wizard's terminal; the harness resets it into its app if it is
// left in the ROM loader.
test('intellex.wizard_reboot_w1_boots_app W1 restarted while the Wizard holds it through Intellex boots its app',
  async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  const a = await args();
  const g = await B.boardGuard(context, rec);
  const READY = a.ready, ROM = a.rom;            // hil/intellex.py WCB_READY, ROM_LOADER_MARKERS
  const link = B.watchLink(page, ['Rebooting now', READY, ...ROM]);
  await B.openWizard(page);
  const n = a.wcb;
  expect(await B.waitPulled(page, n)).toBe(n);
  const dtr = g.signals.filter((s) => s.body && s.body.dataTerminalReady === true && s.status === 200);
  expect(dtr.length, 'the precondition: the Wizard asserted DTR on W1 through the host').toBeGreaterThan(0);
  await meshSettled(page, a.boards);
  await page.evaluate((n) => {
    document.getElementById(`term-pane-input-${n}`).value = '?reboot';
    return sendTerminalCommandTo(n);
  }, n);
  // ?reboot waits for a quiet queue, 20 s at most (CLAUDE.md rule 11).
  await expect.poll(() => 'Rebooting now' in link.seen, { timeout: 30_000,
    message: 'W1 never printed "Rebooting now" after ?reboot' }).toBe(true);
  const after = (m) => (link.seen[m] || 0) > link.seen['Rebooting now'];
  await expect.poll(() => [READY, ...ROM].some(after), { timeout: 30_000,
    message: 'W1 printed neither its app\'s last setup line nor the ROM loader\'s within 30 s of restarting' }).toBe(true);
  await page.waitForTimeout(1500);
  const rom = ROM.filter(after);
  await B.hil.note(`intellex.wizard_reboot_w1_boots_app: after ?reboot with DTR asserted by the Wizard, W1 ` +
    (rom.length ? `printed ${rom.join(' / ')}: the ROM loader` : 'booted its app'));
  expect(rom, '(should) W1 restarted into the ESP32 ROM loader, not its app: the Wizard\'s DTR held GPIO0 low ' +
    'through Intellex (INTELLEX.md finding 16)').toEqual([]);
  expect(after(READY), 'W1 booted its app').toBe(true);
});
