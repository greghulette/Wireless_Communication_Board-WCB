// Needs no board: how the Wizard (Wizard/app.js) connects a board card and talks to the port - the Connect button and
// its modal, BoardConnection's reconnect, its send / collect / idle-wait primitives, the line handler's boot-banner
// sniffer, and waitForBoardReady's probe loop. Medium-risk gaps the 2026-10-12 coverage re-scan found
// (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each
// test's comment). No real port is reachable: navigator.serial is replaced by lib/rescan.js's fakeSerial, whose ports
// record every open, close, signal and write; boards behind a connection are lib/fake.js's fakes or a real
// BoardConnection on a fake port. Runs standalone, in CI, and under the harness by id (s30_wizard.py).
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, install, pullFake } = require('../lib/fake');
const { setup, toastMark, toastsSince, fakeSerial } = require('../lib/rescan');

const VID = { CH: { vid: 0x1a86, pid: 0x55d4 }, CP: { vid: 0x10c4, pid: 0xea60 } };
const ops = (page, id) => page.evaluate((id) => __serial.of(id), id);

test('wizard.connect_fake_modal_and_button Connect on a card opens the modal - Auto-Detect, Select Port Manually, Authorize, "Remote via WCB n" for each connected board, Share port - and Remote arms the relay and pulls through it; the same button then un-manages the relayed board, and disconnects a connected one', async ({ page }) => {
  // boardConnect (app.js:6856-6860), openConnectModal (6455-6495), modalRemoteConnect (6581-6589), modalManualSelect /
  // modalAuthorize / _modalDoConnect (6526-6579), clearRemoteConnected (6665-6698), boardDisconnect (6868-6892). The
  // card's "Remote via WCB n" label (setRemoteConnected writes b<n>-conn-label) has no element in index.html; the status
  // badge is what shows it. Row connect.modal_and_button.
  await setup(page);
  await pullFake(page, 1, board());
  await page.locator('#b2-btn-connect').click();
  await expect(page.locator('#connect-modal.open')).toBeVisible();
  expect(await page.locator('#connect-modal-title').textContent()).toBe('Connect WCB 2');
  const titles = await page.$$eval('#connect-modal .modal-option-title', (els) => els.map((e) => e.textContent.trim().split('\n')[0].trim()));
  expect(titles.slice(1)).toEqual(['Select Port Manually', 'Authorize New Board', 'Remote via WCB 1', 'Share port across tabs']);
  expect(titles[0]).toContain('Auto');

  // Remote via WCB 1: the card reads as remote, and the pull goes out through WCB 1.
  await page.evaluate(() => { __fake.log.length = 0; });
  await page.locator('#connect-modal button.modal-option', { hasText: 'Remote via WCB 1' }).click();
  await expect(page.locator('#connect-modal.open')).toHaveCount(0);
  expect(await page.evaluate(() => [remoteRelayForBoard[2], document.getElementById('b2-status-badge').textContent,
                                    document.getElementById('b2-btn-connect').textContent])).toEqual([1, '📡 Remote', 'Disconnect']);
  await page.waitForFunction(() => __fake.log.some((e) => e.s.includes('MGMT,PULL,2,P')), null, { timeout: 5000 });

  // The same button now un-manages it: the target's terminal is stopped through the relay, and the card is free again.
  await page.evaluate(() => { __fake.log.length = 0; });
  await page.locator('#b2-btn-connect').click();
  expect(await page.evaluate(() => [remoteRelayForBoard[2] ?? null, document.getElementById('b2-btn-connect').textContent])).toEqual([null, 'Connect']);
  await page.waitForFunction(() => __fake.log.some((e) => e.slot === 1 && e.s.includes('RTERM,STOP')), null, { timeout: 5000 });
  await expect(page.locator('#connect-modal.open')).toHaveCount(0);

  // On a connected board it disconnects: no modal, the connection gone, the card back to Connect.
  await page.locator('#b1-btn-connect').click();
  await expect(page.locator('#connect-modal.open')).toHaveCount(0);
  await page.waitForFunction(() => !boardConnections[1]);
  expect(await page.evaluate(() => document.getElementById('b1-btn-connect').textContent)).toBe('Connect');

  // Nothing else connected: no Remote option, Share still offered. Select Port Manually, cancelled, says nothing;
  // a picked port is connected on that slot and pulled 3 s later.
  await setup(page);
  await fakeSerial(page, { A: VID.CH });
  await page.evaluate(() => {
    window.__connects = [];
    window.establishConnection = async (n, port, used) => { __connects.push({ n, port: port.id, used: [...used].map((p) => p.id) }); return null; };
    window.__pulls = [];
    window.boardPull = async (n) => { __pulls.push({ n, at: Date.now() }); };
  });
  await page.locator('#b1-btn-connect').click();
  expect(await page.$$eval('#connect-modal .modal-option-title', (els) => els.map((e) => e.textContent.trim().split('\n')[0].trim())).then((t) => t.slice(1)))
    .toEqual(['Select Port Manually', 'Authorize New Board', 'Share port across tabs']);
  let m = await toastMark(page);
  await page.locator('#connect-modal button.modal-option', { hasText: 'Select Port Manually' }).click();
  await page.waitForFunction(() => __serial.log.some((e) => e.op === 'requestPort'));
  await page.waitForTimeout(200);
  expect(await toastsSince(page, m), 'a cancelled picker is not an error').toBe('');
  expect(await page.evaluate(() => __connects)).toEqual([]);
  await page.evaluate(() => { __serial.serial.pick = __serial.ports.A; });
  await page.locator('#b1-btn-connect').click();
  const t0 = await page.evaluate(() => Date.now());
  await page.locator('#connect-modal button.modal-option', { hasText: 'Authorize New Board' }).click();
  await page.waitForFunction(() => __pulls.length > 0, null, { timeout: 6000 });
  expect(await page.evaluate(() => __connects)).toEqual([{ n: 1, port: 'A', used: [] }]);
  const pullAt = await page.evaluate(() => __pulls[0]);
  expect(pullAt.n).toBe(1);
  expect(pullAt.at - t0, 'pulled 3 s after the connect').toBeGreaterThanOrEqual(2900);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.connect_fake_reconnect_platforms reconnect() reopens the same port with a DTR pulse, or on a Mac takes a fresh handle with the same VID that no other board holds - never another board\'s port - unless told to skip that search; on Windows it retries the close, reuses a port still open from a flash, and takes a port a USB connect event offers', async ({ page }) => {
  // BoardConnection.reconnect (app.js:5595-5775). The Windows branches run with the page's platform reported as
  // Windows (_isWindows is read once, on load). Row connect.reconnect_platforms.
  await setup(page);
  // A: this board's port, gone stale. O: WCB2's port (same VID). D: another VID. F: this board's fresh handle.
  await fakeSerial(page, { O: VID.CH, A: VID.CH, D: VID.CP, F: VID.CH });
  // Each case: the last case's connection stopped (its read loop holds a port), every port closed and reset.
  const fresh = () => page.evaluate(async () => {
    for (const c of Object.values(boardConnections)) { try { await c.closeForReconnect(); } catch (_) {} }
    for (const p of Object.values(__serial.ports)) {
      Object.assign(p, { state: 'closed', stream: null, ctrl: null, _writable: null, openFails: 0, closeFails: 0, openError: 'Failed to open serial port.' });
    }
    __serial.serial.granted = Object.values(__serial.ports);
    __serial.log.length = 0;
  });
  const start = async (opts) => {
    await fresh();
    return page.evaluate(async (opts) => {
      const other = new BoardConnection(2); other.port = __serial.ports.O; other._connected = true; boardConnections[2] = other;
      const c = new BoardConnection(1); c.port = __serial.ports.A; boardConnections[1] = c;
      __serial.ports.A.openFails = 99; __serial.ports.A.openError = 'The device has been lost.';
      const ok = await c.reconnect(3, 50, opts);
      return { ok, port: c.port?.id, connected: c._connected };
    }, opts);
  };

  let r = await start({});
  expect(r).toEqual({ ok: true, port: 'F', connected: true });
  expect(await ops(page, 'O'), 'another board\'s port is never touched').toEqual([]);
  expect(await ops(page, 'D'), 'nor a port of another VID').toEqual([]);
  expect(await ops(page, 'A')).toEqual(['close', 'open']);
  expect(await ops(page, 'F')).toEqual(['close', 'open', 'signals']);
  expect(await page.evaluate(() => __serial.log.filter((e) => e.id === 'F' && e.op === 'signals').map((e) => e.s))).toEqual(['{"dataTerminalReady":true}']);

  // skipFreshPortSearch (a failed flash): only the same handle, and getPorts is never asked.
  r = await start({ skipFreshPortSearch: true });
  expect(r).toEqual({ ok: false, port: 'A', connected: false });
  expect(await page.evaluate(() => __serial.log.filter((e) => e.op === 'getPorts').length)).toBe(0);
  expect(await ops(page, 'F')).toEqual([]);
  expect(await ops(page, 'A')).toEqual(['close', 'open', 'open', 'open']);

  // The same handle comes back: DTR is pulsed false then true, and the board read again.
  await fresh();
  r = await page.evaluate(async () => {
    __serial.serial.granted = [__serial.ports.A];
    const c = new BoardConnection(1); c.port = __serial.ports.A; boardConnections[1] = c;
    __serial.ports.A.openFails = 1;
    const ok = await c.reconnect(3, 50);
    return { ok, signals: __serial.log.filter((e) => e.id === 'A' && e.op === 'signals').map((e) => e.s), reading: !!c.reader };
  });
  expect(r).toEqual({ ok: true, signals: ['{"dataTerminalReady":false}', '{"dataTerminalReady":true}'], reading: true });

  // Windows.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgentData', { configurable: true, get: () => ({ platform: 'Windows' }) });
  });
  await openWizard(page);
  await install(page);
  expect(await page.evaluate(() => _isWindows)).toBe(true);
  await fakeSerial(page, { A: VID.CH, E: VID.CH });
  // A close the driver refuses twice is retried until it goes through, then the port reopened.
  await fresh();
  r = await page.evaluate(async () => {
    const c = new BoardConnection(1); c.port = __serial.ports.A; boardConnections[1] = c;
    __serial.ports.A.state = 'open'; __serial.ports.A.closeFails = 2;
    const ok = await c.reconnect(2, 50);
    return { ok, ops: __serial.of('A') };
  });
  expect(r).toEqual({ ok: true, ops: ['close', 'close', 'close', 'open', 'signals', 'signals'] });
  // Still open after a flash (open says already open, the stream free): reconfigured to 115200 and read, not reopened.
  await fresh();
  r = await page.evaluate(async () => {
    const c = new BoardConnection(1); c.port = __serial.ports.A; boardConnections[1] = c;
    Object.assign(__serial.ports.A, { state: 'open', closeFails: 4, openFails: 1, openError: 'already' });
    const ok = await c.reconnect(2, 50);
    return { ok, log: __serial.log.filter((e) => e.id === 'A').map((e) => e.op + (e.baud ? ` ${e.baud}` : '') + (e.s ? ` ${e.s}` : '')), connected: c._connected };
  });
  expect(r).toEqual({ ok: true, connected: true,
    log: ['close', 'close', 'close', 'close', 'open 115200', 'reconfigure 115200', 'signals {"dataTerminalReady":true}'] });
  // The handle is lost for good, and the USB connect event offers the re-enumerated port: that one is opened.
  await fresh();
  r = await page.evaluate(async () => {
    __serial.serial.granted = [__serial.ports.A];
    Object.assign(__serial.ports.A, { state: 'open', openFails: 99, openError: 'The device has been lost.' });
    const c = new BoardConnection(1); c.port = __serial.ports.A; boardConnections[1] = c;
    setTimeout(() => __serial.fire('connect', 'E'), 20);
    const ok = await c.reconnect(3, 60);
    return { ok, port: c.port.id, e: __serial.of('E'), listeners: (__serial.listeners.connect || []).length };
  });
  expect(r).toEqual({ ok: true, port: 'E', e: ['close', 'open', 'signals'], listeners: 0 });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.connect_fake_wait_board_ready waitForBoardReady keeps one listener, probes with WCB_WEBTOOL_CONFIG_PULL and then <funcChar>backup every 10 s while the board is connected and read, resolves true on the backup\'s end marker, and gives up with false at its timeout', async ({ page }) => {
  // waitForBoardReady (app.js:7358-7426). Row connect.wait_board_ready.
  await setup(page);
  await pullFake(page, 1, board(['FUNCCHAR,!'], { wcbq: 2 }), { lfi: '!' });
  await pullFake(page, 2, board([], { wcb: 2 }));
  const r = await page.evaluate(async () => {
    const t0 = Date.now();
    const [c1, c2] = [boardConnections[1], boardConnections[2]];
    c1.reader = {}; c2.reader = {};
    const before = [c1._dataCallbacks.length, c2._dataCallbacks.length];
    __fake.log.length = 0;
    // WCB1 answers the third probe; WCB2 never does.
    const send = c1.send;
    c1.send = async (s) => {
      await send(s);
      if (String(s).startsWith('WCB_WEBTOOL_CONFIG_PULL') && __fake.sent(1).filter((x) => x.startsWith('WCB_WEBTOOL')).length === 3) {
        setTimeout(() => c1._handleLine('--------- End of Backup ---------'), 300);
      }
    };
    const during = [];
    const p1 = waitForBoardReady(1, c1, { preDelayMs: 0 });
    const p2 = waitForBoardReady(2, c2, { preDelayMs: 0, totalTimeoutMs: 15000 });
    during.push(c1._dataCallbacks.length - before[0], c2._dataCallbacks.length - before[1]);
    const [ok1, ok2] = [await p1, await p2];
    const at = (n) => __fake.log.filter((e) => e.slot === n && e.op === 'send').map((e) => ({ s: e.s, t: e.at - t0 }));
    return { ok1, ok2, during, after: [c1._dataCallbacks.length - before[0], c2._dataCallbacks.length - before[1]], s1: at(1), s2: at(2), took: Date.now() - t0 };
  });
  expect([r.ok1, r.ok2]).toEqual([true, false]);
  expect(r.during, 'one listener each while waiting').toEqual([1, 1]);
  expect(r.after, 'and none left behind').toEqual([0, 0]);
  expect(r.s1.map((x) => x.s)).toEqual(['WCB_WEBTOOL_CONFIG_PULL', '!backup', 'WCB_WEBTOOL_CONFIG_PULL', '!backup', 'WCB_WEBTOOL_CONFIG_PULL', '!backup']);
  const probes = r.s1.filter((x) => x.s.startsWith('WCB_WEBTOOL')).map((x) => x.t);
  expect(probes[0]).toBeLessThan(500);
  expect(probes[1] - probes[0]).toBeGreaterThanOrEqual(9900);
  expect(probes[2] - probes[1]).toBeGreaterThanOrEqual(9900);
  const pair = r.s1.findIndex((x) => x.s === '!backup');
  expect(r.s1[pair].t - r.s1[pair - 1].t, 'the fallback 200 ms after the fixed command').toBeGreaterThanOrEqual(190);
  // WCB2: probed at 0 and 10 s; at 20 s its 15 s are up - no third probe, false.
  expect(r.s2.map((x) => x.s)).toEqual(['WCB_WEBTOOL_CONFIG_PULL', '?backup', 'WCB_WEBTOOL_CONFIG_PULL', '?backup']);

  // Not read (no reader): nothing is sent, and it still gives up on time.
  const r2 = await page.evaluate(async () => {
    const c = boardConnections[2]; c.reader = null; __fake.log.length = 0;
    const ok = await waitForBoardReady(2, c, { preDelayMs: 0, totalTimeoutMs: 5000 });
    return { ok, sent: __fake.sent(2) };
  });
  expect(r2).toEqual({ ok: false, sent: [] });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.connect_fake_send_collect_idle send() writes one at a time and refuses with no open port; sendAndCollect returns on its sentinel or at its timeout; sendAndAwaitIdle returns 250 ms after the last line (ok) and at its hard cap with ok false when nothing answered', async ({ page }) => {
  // BoardConnection.send, sendAndCollect, sendAndAwaitIdle (app.js:5804-5891). Row connect.send_collect_idle.
  await setup(page);
  await fakeSerial(page, { A: VID.CH });
  const r = await page.evaluate(async () => {
    const P = __serial.ports.A;
    await P.open({ baudRate: 115200 });
    const c = new BoardConnection(1); c.port = P; c._connected = true;
    const out = {};
    // Three sends at once on a slow port: written whole, in order, none refused for a locked writer.
    P.writeDelay = 60;
    const errs = [];
    await Promise.all(['one\r', 'two\r', 'three\r'].map((s) => c.send(s).catch((e) => errs.push(e.message))));
    out.writes = P.writes.slice(); out.errs = errs;
    P.writeDelay = 0;
    // Sentinel: lines up to it, then done - well before the timeout; the listener removed.
    const cb0 = c._dataCallbacks.length;
    let t = Date.now();
    setTimeout(() => { c._handleLine('first'); c._handleLine('second END here'); c._handleLine('late'); }, 50);
    out.collect = await c.sendAndCollect('Q1', 3000, 'END');
    out.collectMs = Date.now() - t;
    out.cbLeft = c._dataCallbacks.length - cb0;
    // No sentinel: whatever came, at the timeout.
    t = Date.now();
    setTimeout(() => c._handleLine('only'), 50);
    out.timeout = await c.sendAndCollect('Q2', 600, 'END');
    out.timeoutMs = Date.now() - t;
    // Idle: one line, then quiet.
    t = Date.now();
    setTimeout(() => c._handleLine('ack'), 50);
    out.idle1 = await c.sendAndAwaitIdle('C1', { quietMs: 250, hardTimeoutMs: 3000 });
    out.idle1Ms = Date.now() - t;
    // Lines every 100 ms for 500 ms keep it waiting until 250 ms after the last.
    t = Date.now();
    for (let i = 1; i <= 5; i++) setTimeout(() => c._handleLine(`chat ${i}`), i * 100);
    out.idle2 = await c.sendAndAwaitIdle('C2', { quietMs: 250, hardTimeoutMs: 3000 });
    out.idle2Ms = Date.now() - t;
    // Nothing: ok false at the hard cap.
    t = Date.now();
    out.idle3 = await c.sendAndAwaitIdle('C3', { quietMs: 250, hardTimeoutMs: 700 });
    out.idle3Ms = Date.now() - t;
    out.cmds = P.writes.slice(3);
    // No open port: send refuses, and both waits end at once instead of waiting out their timers.
    c._connected = false;
    out.refused = await c.send('x').then(() => 'sent', (e) => e.message);
    t = Date.now();
    out.dead = [await c.sendAndCollect('Q3', 3000), (await c.sendAndAwaitIdle('C4', { hardTimeoutMs: 3000 })).ok];
    out.deadMs = Date.now() - t;
    return out;
  });
  expect([r.writes, r.errs]).toEqual([['one\r', 'two\r', 'three\r'], []]);
  expect(r.collect).toBe('first\nsecond END here');
  expect(r.collectMs).toBeLessThan(1000);
  expect(r.cbLeft).toBe(0);
  expect([r.timeout, r.timeoutMs >= 590]).toEqual(['only', true]);
  expect(r.idle1).toEqual({ ok: true, lines: ['ack'] });
  expect(r.idle1Ms).toBeGreaterThanOrEqual(290);
  expect(r.idle1Ms).toBeLessThan(1000);
  expect(r.idle2.lines).toEqual(['chat 1', 'chat 2', 'chat 3', 'chat 4', 'chat 5']);
  expect(r.idle2Ms).toBeGreaterThanOrEqual(740);
  expect(r.idle3).toEqual({ ok: false, lines: [] });
  expect(r.idle3Ms).toBeGreaterThanOrEqual(690);
  expect(r.cmds).toEqual(['Q1\r', 'Q2\r', 'C1\r', 'C2\r', 'C3\r']);
  expect(r.refused).toBe('Not connected');
  expect(r.dead).toEqual(['', false]);
  expect(r.deadMs).toBeLessThan(500);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.connect_fake_line_handling the boot banner a board prints is read off the line handler: its function identifier, delimiter and command character go into the board\'s boot characters, its config and the General fields, its software version onto the card; RC heartbeat and stick lines reach no listener', async ({ page }) => {
  // BoardConnection._handleLine's RC filter and boot-banner sniffer (app.js:5899-5980). [TERM:n] routing, listener
  // isolation and the config summaries are wizard.remote_pull_fake_display's and wizard.app_fake_terminal's.
  // Row connect.line_handling.
  await setup(page);
  await pullFake(page, 1, board());
  const r = await page.evaluate(() => {
    const c = boardConnections[1];
    const heard = [];
    c.onData((l) => heard.push(l));
    for (const l of ['{"rc_hb":1,"id":3}', '{"type":"x","rc_ch":[1,2]}', '{"rc_trig":5}', 'Local Function Identifier: !',
                     'Delimeter Character: ~', 'Command Character: /', 'Software Version: 6.2.9_101010ROCT2026', 'an ordinary line']) c._handleLine(l);
    const g = (id) => document.getElementById(id).value;
    const { over, ...chars } = boardBootChars[1];
    return { heard, chars, overIsBaseline: over === boardBaselines[1],
             cfg: [boardConfigs[1].funcChar, boardConfigs[1].delimiter, boardConfigs[1].cmdChar, boardConfigs[1].fwVersion],
             general: [g('g-funcchar'), g('g-delimiter'), g('g-cmdchar'), systemConfig.general.funcChar],
             sw: document.getElementById('b1-sw-version').textContent };
  });
  expect(r.heard).toEqual(['{"rc_trig":5}', 'Local Function Identifier: !', 'Delimeter Character: ~', 'Command Character: /',
                           'Software Version: 6.2.9_101010ROCT2026', 'an ordinary line']);
  expect(r.chars).toEqual({ funcChar: '!', delimiter: '~', cmdChar: '/' });
  expect(r.overIsBaseline).toBe(true);
  expect(r.cfg).toEqual(['!', '~', '/', '6.2.9_101010ROCT2026']);
  expect(r.general).toEqual(['!', '~', '/', '!']);
  expect(r.sw).toContain('6.2.9');
  // The banner's characters are the ones the next command to the board uses.
  expect(await page.evaluate(() => [_liveFuncChar(1), _liveCmdChar(1)])).toEqual(['!', '/']);
  expect(page.wizErrors).toEqual([]);
});
