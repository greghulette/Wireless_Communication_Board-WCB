// Needs no board: the shared-port hub (Wizard/serial-hub.js) and how the Wizard (Wizard/app.js) rides it - a shared
// BoardConnection, the Share-port rebuild, the flash borrow - and a pull that finds a board in the wrong slot. Medium-risk
// gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md, rows in
// docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). The hub runs for real, with
// its Web Lock and BroadcastChannel, on lib/rescan.js's fake ports: two pages of one context are two tabs of one origin,
// and two hubs in one page reach each other the same way. Where a spec stops at a seam of app.js (the hub a shared
// connection uses, flashFirmware) the stub records what it was asked. Runs standalone, in CI, and under the harness by
// id (s30_wizard.py).
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, install, pullFake } = require('../lib/fake');
const { setup, toastMark, toastsSince, fakeSerial } = require('../lib/rescan');

const CH = { vid: 0x1a86, pid: 0x55d4 };
const CP = { vid: 0x10c4, pid: 0xea60 };

// A WcbSerialHub in the page, on its own channel and lock, with everything it emits recorded: __hubs[name].
const hub = (page, name, names) => page.evaluate(({ name, names }) => {
  window.__hubs = window.__hubs || {};
  const h = new WcbSerialHub({ channelName: `hil-${names}`, lockName: `hil-${names}-lock` });
  const rec = { data: '', states: [], logs: [] };
  h.on('data', (u8) => { rec.data += new TextDecoder().decode(u8); });
  h.on('state', (s) => rec.states.push({ role: s.role, portOpen: s.portOpen }));
  h.on('log', (m) => rec.logs.push(m));
  window.__hubs[name] = { h, rec };
}, { name, names });
const held = (page, lock) => page.evaluate(async (lock) => {
  const q = await navigator.locks.query();
  return { held: q.held.filter((l) => l.name === lock).length, pending: q.pending.filter((l) => l.name === lock).length };
}, lock);

test('wizard.hub_fake_protocol the shared-port hub: the tab holding a port leads and opens it, a second tab follows (its sends reach the leader\'s port, the port\'s bytes reach it) and takes over when the leader closes, adopting the one granted port that matches; it declines an ambiguous match, steps down from a port that will not open or goes dead (with a backoff), and releasePortKeepOpen hands the port back open', async ({ page, context }) => {
  // WcbSerialHub (serial-hub.js:72-566): join, adoptPort, send, leave, releasePortKeepOpen, _maybeCampaign / _campaign /
  // _becomeLeader, _stepDownPortless, _adoptGrantedPort, _openAndRead, _readLoop, the bus. Row hub.protocol.
  await setup(page);
  await fakeSerial(page, { A: CH });
  const b = await context.newPage();
  b.wizErrors = [];
  b.on('pageerror', (e) => b.wizErrors.push(e.message));
  await openWizard(b);
  await install(b);
  await fakeSerial(b, { B: CH });

  // Tab 1 holds the port: it leads and opens it.
  await hub(page, 'A', 'p');
  await page.evaluate(async () => { __hubs.A.h.join(); await __hubs.A.h.adoptPort(__serial.ports.A); });
  await page.waitForFunction(() => __hubs.A.h.role === 'leader' && __hubs.A.h.portOpen);
  expect(await page.evaluate(() => __serial.of('A'))).toEqual(['open']);
  // Tab 2 joins with no port: it follows, sees the port open, and queues for the lock (it saw a real leader's port).
  await hub(b, 'B', 'p');
  await b.evaluate(() => __hubs.B.h.join());
  await b.waitForFunction(() => __hubs.B.h.portOpen);
  expect(await b.evaluate(() => [__hubs.B.h.role, __serial.log.filter((e) => e.op === 'open').length])).toEqual(['follower', 0]);
  await expect.poll(() => held(b, 'hil-p-lock')).toEqual({ held: 1, pending: 1 });

  // The follower's send is written by the leader; the port's bytes reach both tabs.
  await b.evaluate(() => __hubs.B.h.send('?PING\r'));
  await page.waitForFunction(() => __serial.ports.A.writes.join('').includes('?PING\r'));
  await page.evaluate(() => __serial.ports.A.feed('hello from the board\r\n'));
  await b.waitForFunction(() => __hubs.B.rec.data.includes('hello from the board\r\n'));
  expect(await page.evaluate(() => __hubs.A.rec.data)).toBe('hello from the board\r\n');

  // The leader's tab closes: the follower is elected, adopts the granted port matching the leader's VID/PID, opens it.
  await page.close();
  await b.waitForFunction(() => __hubs.B.h.role === 'leader' && __hubs.B.h._portOpen, null, { timeout: 10_000 });
  expect(await b.evaluate(() => [__serial.of('B'), __hubs.B.rec.logs.includes('adopted origin-granted port for failover')])).toEqual([['open'], true]);
  await b.evaluate(() => __hubs.B.h.send('after\r'));
  await b.waitForFunction(() => __serial.ports.B.writes.includes('after\r'));
  await b.evaluate(() => __hubs.B.h.leave());

  // One page, two hubs: the same bus and lock as two tabs.
  await fakeSerial(b, { P: CH, Q: CH, BAD: CH, G: CP });
  // An ambiguous failover: three granted ports share the leader's VID/PID, so the promoted follower opens none of them
  // and steps down rather than squat the lock.
  await hub(b, 'X', 'x');
  await hub(b, 'Y', 'x');
  await b.evaluate(async () => { __hubs.X.h.join(); await __hubs.X.h.adoptPort(__serial.ports.P); });
  await b.waitForFunction(() => __hubs.X.h._portOpen);
  await b.evaluate(() => __hubs.Y.h.join());
  await expect.poll(() => held(b, 'hil-x-lock')).toEqual({ held: 1, pending: 1 });
  await b.evaluate(() => { __serial.log.length = 0; return __hubs.X.h.leave(); });
  await b.waitForFunction(() => __hubs.Y.rec.logs.some((l) => l.includes('3 granted ports share the leader\'s VID/PID')), null, { timeout: 5000 });
  await b.waitForFunction(() => __hubs.Y.h.role === 'follower');
  expect(await b.evaluate(() => __serial.log.filter((e) => e.id).map((e) => `${e.id} ${e.op}`)), 'the leader closed its port on leave; nothing opened since')
    .toEqual(['P close']);
  await b.evaluate(() => __hubs.Y.h.leave());

  // A port that will not open: four tries, 300 ms apart, then the lock is released - another holder of a port leads.
  await hub(b, 'D', 'd');
  await hub(b, 'E', 'd');
  await b.evaluate(async () => {
    __serial.log.length = 0;
    __serial.ports.BAD.openFails = 99;
    __hubs.D.h.join();
    await __hubs.D.h.adoptPort(__serial.ports.BAD);
  });
  await b.waitForFunction(() => __hubs.D.rec.logs.some((l) => l.startsWith('open failed')), null, { timeout: 5000 });
  const opens = await b.evaluate(() => __serial.log.filter((e) => e.id === 'BAD' && e.op === 'open').map((e) => e.at));
  expect(opens.length).toBe(4);
  expect(opens[3] - opens[0]).toBeGreaterThanOrEqual(850);
  expect(await b.evaluate(() => __hubs.D.h.role)).toBe('follower');
  await b.evaluate(async () => { __hubs.E.h.join(); await __hubs.E.h.adoptPort(__serial.ports.G); });
  await b.waitForFunction(() => __hubs.E.h.role === 'leader' && __hubs.E.h._portOpen, null, { timeout: 10_000 });
  await b.evaluate(async () => { await __hubs.D.h.leave(); await __hubs.E.h.leave(); });

  // A port that goes dead under the leader (its stream ends at once, six times running): the port is closed, the
  // leader steps down announcing it closed, and re-campaigns only after a backoff.
  await hub(b, 'L', 'l');
  const dead = await b.evaluate(async () => {
    const d = { opens: [], closes: [], state: 'closed' };
    window.__dead = d;
    const port = {
      getInfo: () => ({ usbVendorId: 0x1a86, usbProductId: 0x55d4 }),
      async open() { d.opens.push(Date.now()); d.state = 'open'; },
      async close() { d.closes.push(Date.now()); d.state = 'closed'; },
      get readable() { return d.state === 'open' ? new ReadableStream({ start(c) { c.close(); } }) : null; },
      get writable() { return null; },
    };
    __hubs.L.h.join();
    await __hubs.L.h.adoptPort(port);
    const t0 = Date.now();
    while (d.opens.length < 2 && Date.now() - t0 < 8000) await new Promise((r) => setTimeout(r, 50));
    await __hubs.L.h.leave();
    return { opens: d.opens, closes: d.closes, logs: __hubs.L.rec.logs, states: __hubs.L.rec.states };
  });
  expect(dead.logs).toContain('port read loop ended (unplug?)');
  expect(dead.opens.length).toBe(2);
  expect(dead.closes[0]).toBeGreaterThanOrEqual(dead.opens[0] + 6 * 250 - 300);
  expect(dead.opens[1] - dead.closes[0], 'the step-down backoff').toBeGreaterThanOrEqual(790);
  const firstOpen = dead.states.findIndex((s) => s.portOpen);
  expect(dead.states.slice(firstOpen).some((s) => !s.portOpen && s.role === 'follower'), 'announced closed').toBe(true);

  // releasePortKeepOpen: the port comes back still open, its reader released, and the lock is free.
  await hub(b, 'R', 'r');
  const kept = await b.evaluate(async () => {
    const G = __serial.ports.G;
    Object.assign(G, { state: 'closed', stream: null, ctrl: null });
    __hubs.R.h.join();
    await __hubs.R.h.adoptPort(G);
    const t0 = Date.now();
    while (!__hubs.R.h._portOpen && Date.now() - t0 < 5000) await new Promise((r) => setTimeout(r, 50));
    __serial.log.length = 0;
    const p = await __hubs.R.h.releasePortKeepOpen();
    return { same: p === G, state: G.state, locked: !!G.readable?.locked, ops: __serial.of('G'), role: __hubs.R.h.role };
  });
  expect(kept).toEqual({ same: true, state: 'open', locked: false, ops: [], role: 'idle' });
  expect(await held(b, 'hil-r-lock')).toEqual({ held: 0, pending: 0 });
  expect(b.wizErrors).toEqual([]);
  await b.close();
});

// A stand-in for the hub a shared BoardConnection rides: it records what it is asked and emits what the spec says.
const stubHub = (page, name, { portOpen = true, role = 'leader' } = {}) => page.evaluate(({ name, portOpen, role }) => {
  const s = {
    portOpen, role, _portOpen: portOpen, lockName: `hil-stub-${name}`, joined: 0, left: 0, sent: [], ls: { data: [], state: [], log: [] },
    on(e, cb) { s.ls[e].push(cb); return s; },
    off(e, cb) { s.ls[e] = s.ls[e].filter((f) => f !== cb); return s; },
    join() { s.joined++; return s; },
    leave() { s.left++; },
    async send(d) { s.sent.push(String(d)); },
    async requestPort() {},
    async adoptPort() {},
    emit(e, arg) { for (const cb of s.ls[e]) cb(arg); },
  };
  window.__stubs = window.__stubs || {};
  window.__stubs[name] = s;
}, { name, portOpen, role });

test('wizard.hub_fake_shared_connection a shared BoardConnection is connected while the hub\'s port is open and its card follows the hub\'s state, decodes the hub\'s bytes through the line splitter and sends through the hub; becomeDirect takes the borrowed port over, and becomeShared hands its open port to a new hub without reopening it and remembers the shared slot', async ({ page }) => {
  // BoardConnection.connectShared, leaveShared, becomeDirect, becomeShared (app.js:6023-6098). Row hub.shared_connection.
  await setup(page);
  await fakeSerial(page, { P: CH });
  await stubHub(page, 'h');
  const card = () => page.evaluate(() => [boardConnections[1].isConnected(), document.getElementById('b1-btn-connect').textContent,
                                          document.getElementById('b1-status-badge').textContent]);
  await page.evaluate(() => {
    const c = new BoardConnection(1);
    c.connectShared(__stubs.h);
    boardConnections[1] = c;
    updateConnectionUI(1, true);
    window.__heard = [];
    c.onData((l) => __heard.push(l));
  });
  expect(await page.evaluate(() => [__stubs.h.joined, __stubs.h.ls.data.length, __stubs.h.ls.state.length])).toEqual([1, 1, 1]);
  expect((await card()).slice(0, 2)).toEqual([true, 'Disconnect']);
  // The hub's port closes, and opens again: the card follows.
  await page.evaluate(() => { __stubs.h.portOpen = false; __stubs.h.emit('state', { portOpen: false, role: 'follower' }); });
  expect(await card()).toEqual([false, 'Connect', 'Not Connected']);
  await page.evaluate(() => { __stubs.h.portOpen = true; __stubs.h.emit('state', { portOpen: true, role: 'leader' }); });
  expect((await card()).slice(0, 2)).toEqual([true, 'Disconnect']);
  // Bytes from the hub, cut anywhere, come out as lines; a send goes to the hub.
  await page.evaluate(async () => {
    const enc = (s) => new TextEncoder().encode(s);
    __stubs.h.emit('data', enc('Software Version: 6.2.')); __stubs.h.emit('data', enc('9\r\nsecond li')); __stubs.h.emit('data', enc('ne\r\n'));
    await boardConnections[1].send('?HELLO\r');
  });
  expect(await page.evaluate(() => [__heard, __stubs.h.sent, boardConfigs[1].fwVersion])).toEqual([['Software Version: 6.2.9', 'second line'], ['?HELLO\r'], '6.2.9']);

  // becomeDirect: the hub's listeners detached, the borrowed port owned and read.
  await page.evaluate(async () => {
    await __serial.ports.P.open({ baudRate: 115200 });
    boardConnections[1].becomeDirect(__serial.ports.P);
  });
  expect(await page.evaluate(() => { const c = boardConnections[1];
    return [c._shared, c._hub, c.port.id, c._connected, __stubs.h.ls.data.length, __stubs.h.ls.state.length, __stubs.h.ls.log.length]; }))
    .toEqual([false, null, 'P', true, 0, 0, 0]);
  await page.evaluate(() => __serial.ports.P.feed('direct now\r\n'));
  await page.waitForFunction(() => __heard.includes('direct now'));

  // becomeShared: a new hub adopts the port as it is - no close, no fresh open, so no reset - and leads it.
  await page.evaluate(() => { __serial.log.length = 0; try { localStorage.removeItem('wcbSharedSlot'); } catch (_) {} });
  const r = await page.evaluate(async () => {
    const c = boardConnections[1];
    await c.becomeShared();
    let slot = null; try { slot = localStorage.getItem('wcbSharedSlot'); } catch (_) {}
    return { shared: c._shared, port: c.port, hubPort: c._hub._port?.id, role: c._hub.role, open: c.isConnected(), slot,
             ops: __serial.log.filter((e) => e.id === 'P').map((e) => e.op), isTheHub: c._hub === _sharedHub };
  });
  expect(r).toEqual({ shared: true, port: null, hubPort: 'P', role: 'leader', open: true, slot: '1', ops: ['open'], isTheHub: true });
  // (That one open was refused - the port was already open - and the hub took the port as it was.)
  expect(await page.evaluate(() => __serial.ports.P.state)).toBe('open');
  await page.evaluate(() => __serial.ports.P.feed('through the hub\r\n'));
  await page.waitForFunction(() => __heard.includes('through the hub'));
  await page.evaluate(async () => { await boardConnections[1].send('?BACK\r'); });
  await page.waitForFunction(() => __serial.ports.P.writes.includes('?BACK\r'));
  await page.evaluate(() => _sharedHub.leave());
  expect(page.wizErrors).toEqual([]);
});

test('wizard.hub_fake_share_rebuild Share port first tears down any shared connection - a shared MgmtRelay\'s managed boards and card too - and the old hub, then rebuilds the hub for the new slot; when no tab ever opens the port the slot is torn down again with a warning, and leaves the hub', async ({ page }) => {
  // sharedConnect (app.js:6354-6396), modalSharedConnect (6399-6434). A share that never opened left the connection's
  // listeners on the hub (BoardConnection.disconnect does not leave it), so a later state from that hub redrew the
  // card as connected, and the slot stayed remembered as the shared one: fixed 2026-10-12 (_dropFailedShare,
  // 6439-6453).
  // Row hub.share_rebuild.
  await setup(page);
  await pullFake(page, 19, board(['RELAY,1'], { wcb: 19 }), { shared: true });
  await stubHub(page, 'old');
  await stubHub(page, 'new');
  await page.evaluate(() => {
    const c = boardConnections[19];
    c._hub = __stubs.old;                         // the relay rides the old hub
    __stubs.old.on('state', () => {});
    _sharedHub = __stubs.old;
    addDiscoveredBoards([5]);
    setRemoteConnected(5, 19);
    window.__realGetSharedHub = getSharedHub;
    window.getSharedHub = () => (_sharedHub = _sharedHub || __stubs.new);
  });
  expect(await page.evaluate(() => [[..._relaySlots], !!document.getElementById('relay-card-19'), remoteRelayForBoard[5]])).toEqual([[19], true, 19]);
  await page.evaluate(() => sharedConnect(2));
  expect(await page.evaluate(() => {
    let slot = null; try { slot = localStorage.getItem('wcbSharedSlot'); } catch (_) {}
    return { relay: [!!boardConnections[19], _relaySlots.has(19), !!document.getElementById('relay-card-19')],
             managed: remoteRelayForBoard[5] ?? null, oldLeft: __stubs.old.left, newJoined: __stubs.new.joined,
             conn: [boardConnections[2]._shared, boardConnections[2]._hub === __stubs.new], slot };
  })).toEqual({ relay: [false, false, false], managed: null, oldLeft: 2, newJoined: 1, conn: [true, true], slot: '2' });

  // Share port from the modal, and no tab ever opens the port: torn down, said so, and nothing left on the hub.
  await setup(page);
  await stubHub(page, 'dead', { portOpen: false, role: 'follower' });
  await page.evaluate(() => { renderBoards(2); window.getSharedHub = () => (_sharedHub = _sharedHub || __stubs.dead); });
  const m = await toastMark(page);
  await page.evaluate(() => { openConnectModal(2); return modalSharedConnect(); });
  expect(await toastsSince(page, m)).toContain('WCB 2: shared — no tab has opened the port yet');
  let after = await page.evaluate(() => {
    let slot = null; try { slot = localStorage.getItem('wcbSharedSlot'); } catch (_) {}
    return { conn: 2 in boardConnections, btn: [document.getElementById('b2-btn-connect').textContent, document.getElementById('b2-btn-connect').disabled],
             listeners: __stubs.dead.ls.state.length + __stubs.dead.ls.data.length, left: __stubs.dead.left, slot };
  });
  expect(after).toEqual({ conn: false, btn: ['Connect', false], listeners: 0, left: 1, slot: null });
  // A late state from that hub does not bring the card back.
  await page.evaluate(() => { __stubs.dead.portOpen = true; __stubs.dead.emit('state', { portOpen: true }); });
  expect(await page.evaluate(() => document.getElementById('b2-btn-connect').textContent)).toBe('Connect');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.hub_fake_flash_borrow a flash on the shared port this tab leads borrows it open (releasePortKeepOpen, becomeDirect), flashes it direct and re-shares it after the reconnect; the re-share is skipped when the board did not come back or another board took the shared slot', async ({ page }) => {
  // boardGo's shared-port borrow (app.js:7766-7788) and _reshareAfterFlash (7671-7693). The refusals are
  // wizard.app_fake_hub_flash_refused's. Row hub.flash_borrow.
  await setup(page);
  await page.route('https://api.github.com/**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await pullFake(page, 1, board(), { shared: true });
  await fakeSerial(page, { P: CH });
  const r = await page.evaluate(async () => {
    const c = boardConnections[1];
    const calls = [];
    const P = __serial.ports.P;
    await P.open({ baudRate: 115200 });
    __serial.log.length = 0;
    Object.assign(c._hub, {
      off() {},
      async releasePortKeepOpen() { calls.push({ op: 'release', at: Date.now() }); return P; },
    });
    _sharedHub = c._hub;
    const becomeDirect = c.becomeDirect.bind(c);
    c.becomeDirect = (port) => { calls.push({ op: 'becomeDirect', port: port.id, at: Date.now() }); return becomeDirect(port); };
    c.becomeShared = async () => { calls.push({ op: 'becomeShared', connected: c._connected, port: c.port?.id, at: Date.now() }); };
    window.waitForBoardReady = async () => false;   // the board's answer after the flash is wizard.flash_fake_post_success's
    window.flashFirmware = async (port, hw, opts) => {
      calls.push({ op: 'flash', port: port?.id, shared: c._shared, borrowed: c._wasSharedBeforeFlash, hubCleared: _sharedHub === null, appOnly: opts.appOnly, at: Date.now() });
    };
    await boardGo(1, { mode: 'update' });
    return { calls: calls.map(({ at, ...x }) => x), flag: c._wasSharedBeforeFlash, shared: c._shared, port: c.port?.id, ops: __serial.of('P') };
  });
  expect(r.calls).toEqual([
    { op: 'release' },
    { op: 'becomeDirect', port: 'P' },
    { op: 'flash', port: 'P', shared: false, borrowed: true, hubCleared: true, appOnly: true },
    { op: 'becomeShared', connected: true, port: 'P' },
  ]);
  expect(r.flag, 'the borrow flag cleared').toBe(false);
  // Closed for the flasher (and again after it, which a closed port refuses), then reopened once, DTR pulsed.
  expect(r.ops[0]).toBe('close');
  expect(r.ops.slice(r.ops.indexOf('open')), 'reopened once after the closes').toEqual(['open', 'signals', 'signals']);
  expect(r.ops.filter((o) => o === 'open')).toEqual(['open']);

  // The board did not come back: no re-share, the flag cleared. Another board is the shared port now: stay direct.
  const r2 = await page.evaluate(async () => {
    const c = boardConnections[1];
    let shares = 0;
    c.becomeShared = async () => { shares++; };
    c._wasSharedBeforeFlash = true; c._connected = false;
    await _reshareAfterFlash(c, 1);
    const offline = [shares, c._wasSharedBeforeFlash];
    __fake.conn(3, { shared: true });
    c._wasSharedBeforeFlash = true; c._connected = true; c.port = c.port || __serial.ports.P;
    await _reshareAfterFlash(c, 1);
    return { offline, taken: [shares, c._wasSharedBeforeFlash, c._connected] };
  });
  expect(r2).toEqual({ offline: [0, false], taken: [0, false, true] });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.pull_fake_slot_migration a board pulled in one slot that reports another number moves there, its terminal output with it and the old pane gone; if that slot is already connected it stays where it landed, with a warning, and the other slot is left alone', async ({ page }) => {
  // boardPull's slot migration (app.js:7575-7617), migrateTerminalPane (9986-9997). The move itself and the duplicate
  // warning are wizard.pull_fake_usb_guards'. Row pull.slot_migration.
  await setup(page);
  await page.evaluate(() => { ensureTerminalPane(1); termLog(1, 'HIL-MARKER before the pull', 'sys'); });
  await pullFake(page, 1, board([], { wcb: 4 }));
  const moved = await page.evaluate(() => {
    const out = document.getElementById('term-pane-output-4');
    return { pane1: !!document.getElementById('term-pane-1'), chip1: !!document.getElementById('term-vis-chip-1'), pane4: !!out,
             text4: out?.textContent ?? '', conn: [boardConnections[4]?.boardIndex, 1 in boardConnections], wcb: boardConfigs[4]?.wcbNumber };
  });
  expect([moved.pane1, moved.chip1, moved.pane4]).toEqual([false, false, true]);
  expect(moved.text4).toContain('HIL-MARKER before the pull');
  expect(moved.text4).toContain('WCB_WEBTOOL_CONFIG_PULL');
  expect(moved.text4.indexOf('HIL-MARKER')).toBeLessThan(moved.text4.indexOf('Auto-migrated from slot 1'));
  expect([moved.conn, moved.wcb]).toEqual([[4, false], 4]);

  // Slot 3 already connected to WCB3; slot 1's board reports WCB3 too: it stays in slot 1.
  await setup(page);
  await pullFake(page, 3, board([], { wcb: 3, wcbq: 3 }));
  const before = await page.evaluate(() => JSON.stringify([boardConfigs[3], boardBaselines[3]]));
  const m = await toastMark(page);
  await pullFake(page, 1, board(['LABEL,S5,Second'], { wcb: 3, wcbq: 3 }));
  expect(await toastsSince(page, m)).toContain('WCB 3 detected in slot 1 — slot 3 is already connected, keeping here');
  expect(await page.evaluate(() => [boardConnections[1].boardIndex, boardConfigs[1].wcbNumber, boardConfigs[1].serialPorts[4].label,
                                    !!boardBaselines[1], document.getElementById('b1-status-badge').textContent]))
    .toEqual([1, 3, 'Second', true, '● Connected']);
  expect(await page.evaluate(() => [JSON.stringify([boardConfigs[3], boardBaselines[3]]), boardConnections[3].boardIndex]))
    .toEqual([before, 3]);
  expect(await page.evaluate(() => !!document.getElementById('term-pane-1'))).toBe(true);
  expect(page.wizErrors).toEqual([]);
});
