// Needs no board: how the Wizard (Wizard/app.js) holds on to a board's port - the read loop's own reconnect when a
// port drops, the shared-port decision a connect makes - and the setup wizard's Connect & Push step: its port
// detection and its push-and-verify watcher. The gaps the 2026-10-12 coverage re-scan found
// (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json).
//
// No real port is reachable from these pages: navigator.serial is replaced, once the page has loaded, by a fake whose
// granted ports the spec scripts (fakeSerial below - bytes fed by the test, close() refusing a locked stream and a
// cancelled reader leaving a fresh stream behind, as Chrome's do). Boards behind a port are lib/fake.js's fakes, or a
// real BoardConnection reading the fake port. Where a spec stops at a seam - sharedConnect (the serial-hub.js
// protocol, which nctool.shared_hub_two_pages runs two-page) or establishConnection (wizard.connect_fake_establish_
// autoshare) - the stub records what it was asked and stands in for it. Runs standalone, in CI, and under the harness
// by id (s30_wizard.py).
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, backup, chain, install, pullFake, relayFake } = require('../lib/fake');

// A fake navigator.serial with the ports in `specs` ({ A: { vid, pid }, ... }) granted. window.__serial.ports[id]
// .feed(text) puts bytes on a port's wire, .end() ends its stream as an unplug or a board reset does, .openError
// makes open() throw. Every open, close and setSignals is in window.__serial.log.
async function fakeSerial(page, specs, { granted } = {}) {
  await page.evaluate(({ specs, granted }) => {
    const log = [];
    const ports = {};
    for (const [id, { vid, pid }] of Object.entries(specs)) {
      const p = {
        id, state: 'closed', stream: null, ctrl: null, pending: [], openError: null,
        getInfo: () => ({ usbVendorId: vid, usbProductId: pid }),
        get readable() {
          if (p.state !== 'open') return null;
          if (!p.stream) {
            p.stream = new ReadableStream({
              start(c) { p.ctrl = c; for (const u of p.pending.splice(0)) c.enqueue(u); },
              cancel() { p.ctrl = null; p.stream = null; },     // a cancelled reader leaves a fresh stream behind
            });
          }
          return p.stream;
        },
        async open() {
          log.push({ id, op: 'open', at: Date.now() });
          if (p.openError) throw p.openError;
          if (p.state === 'open') throw new DOMException('The port is already open.', 'InvalidStateError');
          p.state = 'open';
          p.pending = [];
        },
        async close() {
          const locked = !!p.stream?.locked;
          log.push({ id, op: 'close', locked, at: Date.now() });
          if (p.state !== 'open') throw new DOMException('The port is already closed.', 'InvalidStateError');
          if (locked) throw new TypeError('Cannot cancel a locked stream');
          try { await p.stream?.cancel(); } catch (_) {}
          p.state = 'closed'; p.stream = null; p.ctrl = null;
        },
        async setSignals(s) { log.push({ id, op: 'signals', s: JSON.stringify(s), at: Date.now() }); },
        feed(text) {
          if (p.state !== 'open') return;
          const u = new TextEncoder().encode(text);
          if (p.ctrl) p.ctrl.enqueue(u); else p.pending.push(u);
        },
        end() {
          log.push({ id, op: 'end', at: Date.now() });
          if (p.ctrl) { p.ctrl.close(); p.ctrl = null; p.stream = null; }
        },
      };
      ports[id] = p;
    }
    const serial = {
      granted: (granted || Object.keys(ports)).map((k) => ports[k]),
      pick: null,                                   // what requestPort() returns; null = the user cancelled
      async getPorts() { log.push({ op: 'getPorts', at: Date.now() }); return serial.granted.slice(); },
      async requestPort() {
        log.push({ op: 'requestPort', at: Date.now() });
        if (!serial.pick) throw new DOMException('No port selected by the user.', 'NotFoundError');
        return serial.pick;
      },
      addEventListener() {}, removeEventListener() {},
    };
    Object.defineProperty(navigator, 'serial', { value: serial, configurable: true, writable: true, enumerable: false });
    window.__serial = { serial, ports, log, of: (id) => log.filter((e) => e.id === id).map((e) => e.op) };
  }, { specs, granted });
}

const PORTS = { A: { vid: 0x1a86, pid: 0x55d4 }, B: { vid: 0x10c4, pid: 0xea60 }, C: { vid: 0x303a, pid: 0x1001 } };
const toastMark = (page) => page.evaluate(() => __fake.toasts.length);
const toastsSince = (page, m) => page.evaluate((m) => __fake.toasts.slice(m).map((t) => t.message).join('\n'), m);
const port = (page, id, fn, arg) => page.evaluate(({ id, fn, arg }) => __serial.ports[id][fn](arg), { id, fn, arg });

// Record every call to the page's global functions `names` (the page's own calls reach them by name too).
async function spy(page, names) {
  await page.evaluate((names) => {
    window.__calls = window.__calls || [];
    for (const name of names) {
      const real = window[name];
      window[name] = (...a) => {
        __calls.push({ name, a: a.map((x) => (x && typeof x === 'object' ? (x.id ?? '[obj]') : x)), at: Date.now() });
        return real(...a);
      };
    }
  }, names);
}
const calls = (page, name) => page.evaluate((name) => __calls.filter((c) => c.name === name), name);

test('wizard.connect_fake_port_loss_reconnect when a board\'s port drops on its own the read loop reconnects (10 x 1.5 s), re-arms the boards a relay managed - but not one now on direct USB - pulls 3 s later outside the setup wizard, and frees a MgmtRelay\'s card when it cannot get the port back; a managed reboot or a user\'s disconnect starts none of it', async ({ page }) => {
  // BoardConnection._startReading's tail, app.js:6105-6157. Row connect.port_loss_reconnect.
  // A relay in slot 1 managing WCB2 and WCB3, reading a fake port; reconnect is the real one on that port, wrapped to
  // record its arguments (and to fail at once where the spec says so).
  const start = async ({ wizard = false, directDuringWindow = false, fail = false } = {}) => {
    await openWizard(page);
    await relayFake(page, 1, { 2: chain(board([], { wcb: 2, wcbq: 3 })), 3: chain(board([], { wcb: 3, wcbq: 3 })) });
    await fakeSerial(page, { A: PORTS.A });
    await spy(page, ['clearRemoteBoardsForRelay', 'relayManageOne', 'boardPull']);
    await page.evaluate(async ({ wizard, directDuringWindow, fail, text }) => {
      const c = boardConnections[1];
      c.__backup = text;
      c.port = __serial.ports.A;
      await c.port.open();
      const real = BoardConnection.prototype.reconnect.bind(c);
      c.reconnect = async (...a) => {
        __calls.push({ name: 'reconnect', a, at: Date.now() });
        if (directDuringWindow) __fake.conn(3, {});      // WCB3 plugged in over USB while the relay is down
        const ok = fail ? false : await real(...a);
        __calls.push({ name: 'reconnected', a: [ok], at: Date.now() });
        return ok;
      };
      updateConnectionUI(1, true);
      _wizardOpen = wizard;
      c._startReading();
      __fake.log.length = 0;
    }, { wizard, directDuringWindow, fail, text: backup(board([], { wcbq: 3 })) });
  };

  // The relay's port drops: its boards are dropped, the port reopened, and both boards re-armed through it.
  await start();
  let m = await toastMark(page);
  await port(page, 'A', 'end');
  await page.waitForFunction(() => __calls.some((c) => c.name === 'boardPull'), null, { timeout: 10_000 });
  let all = await page.evaluate(() => __calls);
  const at = (name, i = 0) => all.filter((c) => c.name === name)[i];
  expect(at('clearRemoteBoardsForRelay').a).toEqual([1]);
  expect(at('reconnect').a, '10 attempts, 1.5 s apart').toEqual([10, 1500]);
  expect(at('reconnect').at).toBeGreaterThanOrEqual(at('clearRemoteBoardsForRelay').at);
  expect(at('reconnected').a).toEqual([true]);
  expect(all.filter((c) => c.name === 'relayManageOne').map((c) => c.a)).toEqual([[1, 2, false], [1, 3, false]]);
  expect(at('relayManageOne').at).toBeGreaterThanOrEqual(at('reconnected').at);
  expect(at('boardPull').a).toEqual([1]);
  expect(at('boardPull').at - at('reconnected').at, 'the pull waits 3 s').toBeGreaterThanOrEqual(2950);
  expect(await page.evaluate(() => ({ ...remoteRelayForBoard }))).toEqual({ 2: 1, 3: 1 });
  // The same port, closed and opened once more (DTR pulsed), and read again.
  expect(await page.evaluate(() => __serial.of('A'))).toEqual(['open', 'end', 'close', 'open', 'signals', 'signals']);
  // Re-armed through the relay: each board's remote terminal started again (no RTERM,STOP could go out while it was down).
  const sent = await page.evaluate(() => __fake.sent(1));
  expect([...new Set(sent.filter((s) => s.includes('RTERM,START')).map((s) => s.split(',')[2]))].sort()).toEqual(['2', '3']);
  expect(await toastsSince(page, m)).toContain('WCB 1 reconnected');
  expect(await page.evaluate(() => [boardConnections[1].isConnected(), document.getElementById('b1-btn-connect').textContent]))
    .toEqual([true, 'Disconnect']);

  // In the setup wizard no pull is started (the wizard's watcher pulls), and a board that came up on its own USB port
  // meanwhile is left alone rather than relabelled "Remote via WCB 1".
  await start({ wizard: true, directDuringWindow: true });
  await port(page, 'A', 'end');
  await page.waitForFunction(() => __calls.some((c) => c.name === 'reconnected'), null, { timeout: 10_000 });
  await page.waitForTimeout(4000);
  all = await page.evaluate(() => __calls);
  expect(all.filter((c) => c.name === 'boardPull')).toEqual([]);
  expect(all.filter((c) => c.name === 'relayManageOne').map((c) => c.a)).toEqual([[1, 2, false], [1, 3, false]]);
  expect(await page.evaluate(() => [{ ...remoteRelayForBoard }, !!boardConnections[3]?.isConnected()])).toEqual([{ 2: 1 }, true]);

  // A WCB that does not come back: said so; its card and slot stay, shown not connected.
  await start({ fail: true });
  m = await toastMark(page);
  await port(page, 'A', 'end');
  await page.waitForFunction(() => __calls.some((c) => c.name === 'reconnected'), null, { timeout: 10_000 });
  expect(await toastsSince(page, m)).toContain('WCB 1 did not come back — reconnect manually');
  expect(await page.evaluate(() => [!!boardConnections[1], !!document.getElementById('section-board-1'),
                                    document.getElementById('b1-btn-connect').textContent, { ...remoteRelayForBoard }]))
    .toEqual([true, true, 'Connect', {}]);

  // A MgmtRelay that does not come back: its slot and card are freed, so a real WCB of that number can surface.
  await openWizard(page);
  await pullFake(page, 1, board(['RELAY,1'], { wcb: 19 }));
  await fakeSerial(page, { A: PORTS.A });
  await spy(page, ['clearRemoteBoardsForRelay']);
  await page.evaluate(async () => {
    addDiscoveredBoards([5]);
    setRemoteConnected(5, 19);
    const c = boardConnections[19];
    c.port = __serial.ports.A;
    await c.port.open();
    c.reconnect = async (...a) => { __calls.push({ name: 'reconnect', a, at: Date.now() }); return false; };
    c._startReading();
  });
  expect(await page.evaluate(() => [[..._relaySlots], !!document.getElementById('relay-card-19')])).toEqual([[19], true]);
  m = await toastMark(page);
  await port(page, 'A', 'end');
  await page.waitForFunction(() => !_relaySlots.has(19), null, { timeout: 10_000 });
  expect(await page.evaluate(() => ({ card: !!document.getElementById('relay-card-19'), conn: !!boardConnections[19],
                                      managed: { ...remoteRelayForBoard }, cleared: __calls.filter((c) => c.name === 'clearRemoteBoardsForRelay').map((c) => c.a) })))
    .toEqual({ card: false, conn: false, managed: {}, cleared: [[19]] });
  expect(await toastsSince(page, m)).toContain('WCB 19 did not come back');

  // A reboot boardGo manages (_rebootManaged) and a user's disconnect end the read loop without any of the above.
  for (const [what, set] of [['a managed reboot', (c) => { c._rebootManaged = true; }], ['a disconnect', (c) => { c._connected = false; }]]) {
    await start();
    await page.evaluate(`(${set.toString()})(boardConnections[1])`);
    await port(page, 'A', 'end');
    await page.waitForTimeout(2500);
    expect.soft(await page.evaluate(() => __calls.map((c) => c.name)), what).toEqual([]);
    expect.soft(await page.evaluate(() => ({ ...remoteRelayForBoard })), what).toEqual({ 2: 1, 3: 1 });
  }
  expect(page.wizErrors).toEqual([]);
});

// sharedConnect as a recorder: a shared connection on a stub hub whose portOpen is window.__hubOpen and whose leader
// announces window.__leader ({ usbVendorId, usbProductId }, or null for a leader that never says). It awaits once
// before it installs the connection, as the real one awaits the hub.
async function stubShare(page) {
  await page.evaluate(() => {
    window.__shared = [];
    window.__hubOpen = true;
    window.__leader = null;
    window.sharedConnect = async (n, port) => {
      __shared.push({ n, port: port?.id ?? null });
      await new Promise((r) => setTimeout(r, 30));
      const c = new BoardConnection(n);
      c._shared = true;
      c._hub = { portOpen: __hubOpen, role: 'leader', _leaderPortOpen: !!__leader, _leaderPortInfo: __leader };
      c.leaveShared = () => { __shared.push({ n, left: true }); c._shared = false; c._hub = null; };
      boardConnections[n] = c;
      _sharedHub = { leave() { __shared.push({ hubLeft: true }); } };
      try { localStorage.setItem('wcbSharedSlot', String(n)); } catch (_) {}
      return c;
    };
  });
}

// Hold the shared hub's Web Lock, as another tab leading the hub does. Resolves once it is held.
const holdHubLock = (page) => page.evaluate(() => new Promise((held) => {
  navigator.locks.request('wcb-shared-serial-owner', () => { held(); return new Promise((r) => { window.__releaseHub = r; }); });
}));

// establishConnection(n, port id, allowShare) -> what came of it.
const connect = (page, n, id, allowShare = true) => page.evaluate(async ({ n, id, allowShare }) => {
  try {
    const c = await establishConnection(n, __serial.ports[id], new Set(), allowShare);
    return { shared: !!c._shared, slot: boardConnections[n] === c, port: c._shared ? null : c.port?.id };
  } catch (e) { return { error: e.message, slot: !!boardConnections[n] }; }
}, { n, id, allowShare });

test('wizard.connect_fake_establish_autoshare the first board a page connects becomes the shared port unless another tab leads the hub or the bulk auto-detect opted out, two connects at once share only one, a share that never opens falls back to a direct connection; a port that will not open joins another tab\'s share only when that port is surely the one it holds (the only one granted, or this slot\'s before a refresh), and backs out when the leader\'s board differs', async ({ page }) => {
  // establishConnection, app.js:6215-6321. Two connects at once both took the share - the claim was read before the
  // lock query's await and never again after it - until 2026-10-12. Row connect.establish_autoshare.
  const fresh = async (opts = {}) => {
    await openWizard(page);
    await install(page);
    await fakeSerial(page, { A: PORTS.A, B: PORTS.B }, opts);
    await stubShare(page);
  };

  // The first board is shared; the next goes direct on its own port.
  await fresh();
  let m = await toastMark(page);
  expect(await connect(page, 1, 'A')).toEqual({ shared: true, slot: true, port: null });
  expect(await toastsSince(page, m)).toContain('WCB 1 is the shared port');
  expect(await connect(page, 2, 'B')).toEqual({ shared: false, slot: true, port: 'B' });
  expect(await page.evaluate(() => [__shared, __serial.of('A'), __serial.of('B').slice(0, 2)]))
    .toEqual([[{ n: 1, port: 'A' }], [], ['open', 'signals']]);

  // The bulk auto-detect opts out: direct, nothing shared.
  await fresh();
  expect(await connect(page, 1, 'A', false)).toEqual({ shared: false, slot: true, port: 'A' });
  expect(await page.evaluate(() => __shared)).toEqual([]);

  // Another tab leads the hub: joining would follow ITS port, so this one goes direct.
  await fresh();
  await holdHubLock(page);
  expect(await connect(page, 1, 'A')).toEqual({ shared: false, slot: true, port: 'A' });
  expect(await page.evaluate(() => __shared)).toEqual([]);
  await page.evaluate(() => window.__releaseHub());

  // Two connects at once (the parallel-connect claim): one is shared, the other direct.
  await fresh();
  const both = await page.evaluate(async () => {
    const r = await Promise.all([establishConnection(1, __serial.ports.A), establishConnection(2, __serial.ports.B)]);
    return { shared: r.map((c) => !!c._shared), calls: __shared.filter((s) => s.port).map((s) => s.n), ones: [1, 2].map((n) => !!boardConnections[n]) };
  });
  expect(both.calls, 'one shared connect').toHaveLength(1);
  expect(both.shared.filter(Boolean)).toHaveLength(1);
  expect(both.ones).toEqual([true, true]);

  // A share that never opens: torn down after ~3 s, its slot marker dropped, and the board connected direct.
  await fresh();
  await page.evaluate(() => { window.__hubOpen = false; });
  const t0 = Date.now();
  expect(await connect(page, 1, 'A')).toEqual({ shared: false, slot: true, port: 'A' });
  expect(Date.now() - t0).toBeGreaterThanOrEqual(2900);
  expect(await page.evaluate(() => [__shared, localStorage.getItem('wcbSharedSlot')]))
    .toEqual([[{ n: 1, port: 'A' }, { n: 1, left: true }, { hubLeft: true }], null]);

  // The port will not open because another tab holds it (Wizard refreshed, NaviCore took the port over).
  const busy = () => page.evaluate(() => { __serial.ports.A.openError = new DOMException('The port is already open.', 'InvalidStateError'); });
  // Only one port granted: it is the one the leader holds - join.
  await fresh({ granted: ['A'] });
  await busy();
  await holdHubLock(page);
  m = await toastMark(page);
  expect(await connect(page, 1, 'A')).toEqual({ shared: true, slot: true, port: null });
  expect(await toastsSince(page, m)).toContain('joining the shared session');
  await page.evaluate(() => window.__releaseHub());
  // Two granted and nothing says which: the open error stands, nothing joined.
  await fresh();
  await busy();
  await holdHubLock(page);
  expect(await connect(page, 1, 'A')).toEqual({ error: 'The port is already open.', slot: false });
  expect(await page.evaluate(() => __shared)).toEqual([]);
  await page.evaluate(() => window.__releaseHub());
  // Two granted, this slot was the shared one before the refresh, and the leader's board is this port's: join.
  for (const [what, leader, want] of [
    ['the leader\'s board matches', PORTS.A, { shared: true, slot: true, port: null }],
    ['the leader never says (best effort)', null, { shared: true, slot: true, port: null }],
    ['the leader holds a different board', PORTS.B, { error: 'The port is already open.', slot: false }],
  ]) {
    await fresh();
    await busy();
    await holdHubLock(page);
    await page.evaluate((leader) => {
      localStorage.setItem('wcbSharedSlot', '1');
      window.__leader = leader && { usbVendorId: leader.vid, usbProductId: leader.pid };
    }, leader);
    m = await toastMark(page);
    expect.soft(await connect(page, 1, 'A'), what).toEqual(want);
    if (want.error) {
      expect.soft(await toastsSince(page, m), what).toContain('That port is a different board than the shared session holds — not joining.');
      expect.soft(await page.evaluate(() => __shared), what).toEqual([{ n: 1, port: 'A' }, { n: 1, left: true }, { hubLeft: true }]);
    }
    await page.evaluate(() => window.__releaseHub());
  }
  // One port granted but no tab leads the hub (another app holds the port, so the auto-share's own open never
  // succeeds either and it falls back to direct after ~3 s), or an error that is not about the port being busy: no join.
  await fresh({ granted: ['A'] });
  await busy();
  await page.evaluate(() => { window.__hubOpen = false; });
  expect(await connect(page, 1, 'A')).toEqual({ error: 'The port is already open.', slot: false });
  expect(await page.evaluate(() => __shared), 'only the auto-share attempt, torn down')
    .toEqual([{ n: 1, port: 'A' }, { n: 1, left: true }, { hubLeft: true }]);
  await fresh({ granted: ['A'] });
  await page.evaluate(() => { __serial.ports.A.openError = new DOMException('The device has been lost.', 'NetworkError'); });
  await holdHubLock(page);
  expect(await connect(page, 1, 'A')).toEqual({ error: 'The device has been lost.', slot: false });
  expect(await page.evaluate(() => __shared)).toEqual([]);
  await page.evaluate(() => window.__releaseHub());
  expect(page.wizErrors).toEqual([]);
});

// The page's own setup wizard, on its Connect & Push step, `quantity` WCBs. Each board gets a label of its own on S2
// and HW 2.4; General MAC octet 3 is 4C (lib/fake.js's boards say 4B), so a push of the wizard's config is told apart
// from the board's own. The step opens slot 1's port panel, as it does for a user.
async function wizardAtConnect(page, quantity) {
  await page.evaluate((quantity) => {
    openWizard();
    const b = (n) => { const x = wizardDefaultBoard(n); x.hwVersion = 24; x.serialPorts[1].label = `Wiz${n}`; return x; };
    Object.assign(wizardState, { quantity, password: 'dummy_wizard_password', mac2: '05', mac3: '4C', controlSystem: 'other',
                                 maestroEnabled: false, boards: Array.from({ length: quantity }, (_, i) => b(i + 1)) });
    wizardState.steps = buildWizardSteps();
    wizardState.currentIdx = wizardState.steps.indexOf('connect');
    wizardRenderStep();
  }, quantity);
  await page.waitForFunction(() => wizardState.connectMode === 'seq' && /Detect/.test(document.getElementById('wiz-port-panel-1')?.textContent || ''));
}
const status = (page, n) => page.evaluate((n) => {
  const el = document.getElementById(`wiz-connect-status-${n}`);
  return [el.textContent, el.className.replace('wizard-connect-status', '').trim()];
}, n);

// Every boardGo call from here on, with whether the board's baseline was null (a full push) when it was called.
async function recordGo(page) {
  await page.evaluate(() => {
    window.__realBoardGo = boardGo;
    window.__goCalls = [];
    window.boardGo = (n, opts = {}) => {
      __goCalls.push({ n, baselineNull: boardBaselines[n] === null, at: Date.now() });
      return window.__realBoardGo(n, opts);
    };
  });
}

test('wizard.wiz_fake_watch_push_verify once a wizard board is connected and pulled, the watcher puts the wizard\'s config and General back, pushes it whole, waits for the reconnect, verifies with up to three pulls, and says Done - advancing to the next WCB and closing the wizard once all are done - or Not pushed, Push incomplete, Unverified or No reconnect', async ({ page }) => {
  // wizardWatchForConnect, app.js:12853-12995 (with wizardSetConnectStatus, wizardCheckAllDone, wizardSeqAdvance).
  // The board is lib/fake.js's: connected, pulled (so the watcher's gate opens at once), its push ACKed, its reboot a
  // close and an immediate reconnect. Row wiz.watch_push_verify.
  const DEAD = { ok: false, lines: [] };
  const start = async (quantity, { snapshotVars = null, acks = {}, reconnects = true } = {}) => {
    await openWizard(page);
    await install(page);
    await wizardAtConnect(page, quantity);
    // The wizard's config and General as the step snapshots them before a board connects (_wizPortOpenPanel).
    await page.evaluate((vars) => {
      window.__snap = { cfg: JSON.parse(JSON.stringify(boardConfigs[1])), gen: captureGeneralDOMSnapshot() };
      if (vars) __snap.cfg.variables = vars;
    }, snapshotVars);
    // The board connects and is pulled: its own config, and General from it (MAC octet 3 4B).
    await pullFake(page, 1, board(['LABEL,S2,BoardLabel'], { wcbq: quantity }), { acks });
    await page.evaluate((reconnects) => {
      const c = boardConnections[1];
      if (!reconnects) c.reconnect = async () => { __fake.log.push({ slot: 1, op: 'reconnect', s: 'false', at: Date.now() }); c._connected = false; return false; };
      __fake.log.length = 0;
    }, reconnects);
    await recordGo(page);
    await page.evaluate(() => wizardWatchForConnect(1, __snap.cfg, __snap.gen));
  };
  const settled = (timeout = 45_000) => page.waitForFunction(() => /^[✓✕]/.test(document.getElementById('wiz-connect-status-1').textContent),
                                                            null, { timeout });
  const pulls = () => page.evaluate(() => __fake.log.filter((e) => e.op === 'collect' && e.s === 'WCB_WEBTOOL_CONFIG_PULL').map((e) => e.at));

  // Done: two WCBs, the first one pushed and verified.
  await start(2);
  await settled();
  expect(await status(page, 1)).toEqual(['✓ Done', 'ok']);
  const log = await page.evaluate(() => __fake.log.filter((e) => e.slot === 1).map((e) => ({ op: e.op, s: e.s, at: e.at })));
  expect(await page.evaluate(() => __goCalls.map(({ n, baselineNull }) => ({ n, baselineNull }))), 'one full push').toEqual([{ n: 1, baselineNull: true }]);
  const pushed = log.filter((e) => e.op === 'await').map((e) => e.s);
  expect(pushed).toEqual(expect.arrayContaining(['?WCB,1', '?HW,24', '?LABEL,S2,Wiz1', '?MAC,3,4C']));
  expect(pushed.filter((s) => s.includes('BoardLabel') || s === '?MAC,3,4B'), 'not the board\'s own config').toEqual([]);
  const reopened = log.find((e) => e.op === 'reconnect');
  const verify = log.find((e) => e.op === 'collect' && e.at > reopened.at);
  expect(verify.at - reopened.at, 'the verify pull 8 s after the board is back').toBeGreaterThanOrEqual(7950);
  expect(await page.evaluate(() => [wizardState.connectSeqN, !!wizardConnectWatchers[1],
                                    document.getElementById('wiz-connect-row-2').classList.contains('wiz-connect-dimmed'),
                                    document.getElementById('wizard-modal').classList.contains('open')]))
    .toEqual([2, false, false, true]);
  // The last WCB done too: the wizard closes itself 2.5 s later.
  await page.evaluate(() => wizardSetConnectStatus(2, 'ok', '✓ Done'));
  await page.waitForTimeout(1500);
  expect(await page.evaluate(() => document.getElementById('wizard-modal').classList.contains('open')), 'not before 2.5 s').toBe(true);
  await page.waitForTimeout(1500);
  expect(await page.evaluate(() => [document.getElementById('wizard-modal').classList.contains('open'), _wizardOpen])).toEqual([false, false]);

  // Not pushed: the wizard's config has a variable the push refuses, so nothing is sent. One WCB: nothing advances,
  // the wizard stays open, and Close is offered.
  await start(1, { snapshotVars: [{ name: 'bad name', value: 1 }] });
  let m = await toastMark(page);
  await settled();
  expect(await status(page, 1)).toEqual(['✕ Not pushed', 'err']);
  expect(await toastsSince(page, m)).toContain('WCB 1: nothing was pushed — invalid variable name — nothing was sent');
  expect(await page.evaluate(() => __fake.log.filter((e) => e.op === 'await' || e.op === 'send').length)).toBe(0);
  await page.waitForTimeout(3000);
  expect(await page.evaluate(() => [wizardState.connectSeqN, document.getElementById('wizard-modal').classList.contains('open'),
                                    document.getElementById('wizard-next-btn').textContent, document.getElementById('wizard-next-btn').style.display]))
    .toEqual([1, true, '✓ Close', '']);

  // Push incomplete: one setting never answered, the rest sent.
  await start(1, { acks: { '?LABEL,S2,Wiz1': [DEAD, DEAD] } });
  await settled();
  expect(await status(page, 1)).toEqual(['✕ Push incomplete', 'err']);

  // Unverified: pushed and back, but its ?backup never comes whole - three pulls, 8, 5 and 5 s apart.
  await start(1);
  await page.waitForFunction(() => __fake.log.some((e) => e.s === '?reboot'));
  await page.evaluate(() => {
    boardConnections[1].__backup = '*** WCB Configuration Backup\r\n?HW,24\r\n';       // cut short: no end marker
  });
  await settled();
  expect(await status(page, 1)).toEqual(['✕ Unverified', 'err']);
  const p = await pulls();
  expect(p, 'three verify pulls').toHaveLength(3);
  expect([p[1] - p[0], p[2] - p[1]].every((d) => d >= 4950), JSON.stringify(p)).toBe(true);

  // No reconnect: the board never comes back after its reboot (30 s), so it is not verified at all.
  await start(1, { reconnects: false });
  await settled(60_000);
  expect(await status(page, 1)).toEqual(['✕ No reconnect', 'err']);
  expect(await pulls()).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wiz_fake_connect_step the setup wizard\'s Connect & Push row: Detect opens every free granted port, ignores the 3 s of resets the opens cause (and the 4 s more an RTCWDT board needs), takes the port a POWERON_RESET or Reset reason then comes from, connects it, clears a stale baseline so the push waits for the pull 3 s later, and starts the watcher; Select by COM# connects the picked port, + Authorize only grants one; Cancel disconnects and offers Reconnect; 30 s with no reset gives the buttons back', async ({ page }) => {
  // app.js:12329-12851 (wizPortRenderPanel, wizPortDetect, wizPortUse, wizPortAuthorize, wizPortComPicker,
  // _wizPortOpenPanel, wizPortComplete, wizardCancelConnect, wizardManualConnect). Row wiz.connect_step, its fakeable
  // half: the ports are fakes whose bytes this spec feeds. The other half stays ATTENDED - a real reset press on a real
  // board, and what real hardware and Chrome do around it (the DTR reset an open causes, an RTCWDT board dropping and
  // re-enumerating its USB port mid-drain, which hands Detect a new SerialPort object). establishConnection is a
  // recorder here (wizard.connect_fake_establish_autoshare covers it) that installs a lib/fake.js board on the port.
  const start = async ({ stale = false } = {}) => {
    await openWizard(page);
    await install(page);
    await fakeSerial(page, PORTS);
    await wizardAtConnect(page, 2);
    await page.evaluate(({ stale, text }) => {
      window.__est = [];
      window.establishConnection = async (n, port, usedPorts, allowShare) => {
        __est.push({ n, port: port.id, used: [...usedPorts].map((p) => p.id ?? '?'), allowShare: allowShare ?? null, at: Date.now() });
        const c = __fake.conn(n, { backup: text });
        c.port = port;
        return c;
      };
      // Slot 2 already holds port C, so slot 1's Detect must leave it alone.
      __fake.conn(2, {}).port = __serial.ports.C;
      if (stale) {
        // A board from an earlier session in slot 1: connected (Detect closes it), pulled, a baseline left over.
        __fake.conn(1, {});
        boardBaselines[1] = WCBParser.parseBackupString(text);
      }
      __fake.log.length = 0;
    }, { stale, text: backup(board(['LABEL,S2,BoardLabel'])) });
  };
  const panel = (n = 1) => page.evaluate((n) => document.getElementById(`wiz-port-panel-${n}`).textContent.replace(/\s+/g, ' ').trim(), n);
  const click = (sel, text) => page.evaluate(({ sel, text }) => {
    const b = [...document.querySelectorAll(sel)].find((x) => x.textContent.includes(text));
    if (!b) throw new Error(`no ${text} button in ${sel}`);
    b.click();
  }, { sel, text });
  const est = () => page.evaluate(() => __est.map(({ n, port, used }) => ({ n, port, used })));

  // The row offers its three ways in.
  await start({ stale: true });
  for (const b of ['Detect', '+ Authorize', 'Select by COM#']) expect(await panel()).toContain(b);

  // Detect: A and B opened (C is slot 2's), a reset during the drain ignored, the reset after it taken.
  await click('#wiz-port-panel-1 button', 'Detect');
  await page.waitForFunction(() => __serial.log.filter((e) => e.op === 'open').length === 2);
  expect(await panel()).toContain('Preparing… please wait');
  expect(await page.evaluate(() => __fake.log.filter((e) => e.slot === 1 && e.op === 'close').length), 'the stale connection closed first').toBe(1);
  await page.waitForTimeout(500);
  await port(page, 'A', 'feed', 'ets Jul 29 2019 12:21:46\r\nrst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n');
  await page.waitForTimeout(2900);
  expect(await panel()).toContain('Press the reset button on WCB 1 now!');
  expect(await est(), 'the drain\'s reset picked nothing').toEqual([]);
  await port(page, 'B', 'feed', 'ets Jul 29 2019 12:21:46\r\nrst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n');
  await page.waitForFunction(() => __est.length === 1);
  const connectedAt = await page.evaluate(() => __est[0].at);
  expect(await est()).toEqual([{ n: 1, port: 'B', used: ['C'] }]);
  expect(await page.evaluate(() => [boardBaselines[1], !!wizardConnectWatchers[1]]), 'the stale baseline gone, the watcher on').toEqual([undefined, true]);
  await page.waitForTimeout(300);
  const io = await page.evaluate(() => ({ C: __serial.of('C'), closes: __serial.log.filter((e) => e.op === 'close'),
                                         A: __serial.ports.A.state, B: __serial.ports.B.state }));
  expect(io.C, 'slot 2\'s port untouched').toEqual([]);
  expect([io.A, io.B], 'Detect leaves no port open').toEqual(['closed', 'closed']);
  expect(io.closes.filter((e) => e.locked), 'never a close on a locked stream').toEqual([]);
  // The pull 3 s after the connect; the push only after it (with the old baseline it fired ~0.5 s in, racing the pull).
  await page.waitForFunction(() => __fake.log.some((e) => e.slot === 1 && e.op === 'await'), null, { timeout: 10_000 });
  const l1 = await page.evaluate(() => __fake.log.filter((e) => e.slot === 1).map((e) => ({ op: e.op, s: e.s, at: e.at })));
  const pull = l1.find((e) => e.op === 'collect' && e.s === 'WCB_WEBTOOL_CONFIG_PULL');
  expect(pull.at - connectedAt, 'the pull 3 s after the connect').toBeGreaterThanOrEqual(2950);
  expect(l1.find((e) => e.op === 'await').at).toBeGreaterThan(pull.at);

  // An RTCWDT board: its boot during the drain holds the row in Preparing 4 s more, and a reset in that time is ignored.
  await start();
  await click('#wiz-port-panel-1 button', 'Detect');
  await page.waitForTimeout(500);
  await port(page, 'B', 'feed', 'rst:0x10 (RTCWDT_RTC_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n');
  await page.waitForTimeout(2800);
  expect(await panel(), 'absorbing the auto-reset').toContain('Preparing… please wait');
  await page.waitForTimeout(700);
  await port(page, 'B', 'feed', 'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n');
  await page.waitForTimeout(3500);
  expect(await est(), 'nothing taken while absorbing').toEqual([]);
  expect(await panel()).toContain('Press the reset button on WCB 1 now!');
  await port(page, 'B', 'feed', 'Reset reason: POWERON\r\n');
  await page.waitForFunction(() => __est.length === 1);
  expect((await est())[0].port).toBe('B');

  // Use a port directly (wizPortUse; the panel's port list is gone, the call remains): the same connect, baseline cleared.
  await start();
  await page.evaluate(() => { boardBaselines[1] = { stale: true }; return wizPortUse(1, 1); });
  expect(await page.evaluate(() => [__est.map((e) => e.port), boardBaselines[1]])).toEqual([['B'], undefined]);

  // Select by COM#: the picker's port is connected. + Authorize in the wizard only grants (Detect then finds it).
  // A picker the user cancels changes nothing.
  await start();
  await page.evaluate(() => { __serial.serial.pick = __serial.ports.A; });
  await click('#wiz-port-panel-1 button', 'Select by COM#');
  await page.waitForFunction(() => __est.length === 1);
  expect((await est())[0].port).toBe('A');
  await start();
  await page.evaluate(() => { __serial.serial.pick = __serial.ports.B; });
  await click('#wiz-port-panel-1 button', '+ Authorize');
  await page.waitForTimeout(500);
  expect(await page.evaluate(() => [__serial.log.filter((e) => e.op === 'requestPort').length, __est.length])).toEqual([1, 0]);
  expect(await panel()).toContain('Detect');
  await page.evaluate(() => { __serial.serial.pick = null; });
  await click('#wiz-port-panel-1 button', 'Select by COM#');
  await page.waitForTimeout(500);
  expect(await page.evaluate(() => __est.length)).toBe(0);

  // Cancel after a connect: the watcher stops, the board is disconnected, Reconnect offered; Reconnect reopens the panel.
  await start();
  await page.evaluate(() => wizPortUse(1, 0));
  await page.evaluate(() => wizardCancelConnect(1));
  expect(await page.evaluate(() => [!!wizardConnectWatchers[1], !!boardConnections[1], document.getElementById('wiz-connect-btns-1').style.display,
                                    document.getElementById('wiz-port-panel-1').style.display])).toEqual([false, false, 'flex', 'none']);
  expect(await status(page, 1)).toEqual(['Cancelled', '']);
  await page.waitForTimeout(3500);
  expect(await page.evaluate(() => __fake.log.filter((e) => e.slot === 1 && (e.op === 'await' || e.op === 'collect')).length), 'nothing after the cancel').toBe(0);
  await click('#wiz-connect-btns-1 button', 'Reconnect');
  await page.waitForFunction(() => document.getElementById('wiz-port-panel-1').style.display === '');
  expect(await panel()).toContain('Detect');
  expect(await status(page, 1)).toEqual(['Select port…', '']);

  // Cancel during a Detect closes its ports and gives the buttons back.
  const est0 = await page.evaluate(() => __est.length);
  await click('#wiz-port-panel-1 button', 'Detect');
  await page.waitForFunction(() => __serial.log.filter((e) => e.op === 'open').length === 2);
  await click('#wiz-port-panel-1 button', 'Cancel');
  await page.waitForFunction(() => /Detect/.test(document.getElementById('wiz-port-panel-1').textContent));
  expect(await page.evaluate(() => [__serial.ports.A.state, __serial.ports.B.state, __est.length])).toEqual(['closed', 'closed', est0]);

  // No reset within 30 s: the ports are closed and the buttons come back.
  await start();
  await click('#wiz-port-panel-1 button', 'Detect');
  await page.waitForTimeout(29_000);
  expect(await panel()).toContain('Press the reset button');
  await page.waitForFunction(() => /Detect/.test(document.getElementById('wiz-port-panel-1').textContent), null, { timeout: 5000 });
  expect(await page.evaluate(() => [__serial.ports.A.state, __serial.ports.B.state, __est.length])).toEqual(['closed', 'closed', 0]);
  expect(page.wizErrors).toEqual([]);
});
