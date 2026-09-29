// Needs no board: the Wizard's own logic and panels in app.js (docs/hil_plan/WCB.md WCB-WP41) - the controller selector,
// the relay's ETM listener, the WDP mesh panel and its 12 s discovery tick, ACK pacing, the port-claim guard on the
// broadcast boxes, the terminal, the setup wizard, the system file, the identity fields, the firmware-update check and
// the MgmtRelay card. Fake boards in the real page (lib/fake.js); only the wire is recorded. Runs standalone, in CI,
// and under the harness by id (s30_wizard.py).
//
// (should) tests assert what the Wizard ought to do and fail until it does (test.fail: CI stays green; the harness
// reports them FAIL). Each names its W-row in docs/hil_plan/WCB.md §3.
//
// The WDP dump below is built from the firmware's own printf formats (WCB_WDP.cpp: [WDP:...] :1786 and :1823,
// [WDPIF:...] :1791/:1832, [WDPDA:...] :1215 and :1522, [WDPX:...] :1619, [WDPSEQ:...] :1797, [WDPPWM:...] :1805/:1845,
// [WDPCFG:...] :1850, [WDP:END] :1852). The plan asked for one captured from W1; this worktree may not touch the bench,
// and the formats are what a capture would contain.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { board, backup, chain, install, pullFake, push, edit, relayFake } = require('../lib/fake');

const FW = '6.2.1_290236RSEP2026';
const dumpFrom = (rows) => [...rows, '[WDPCFG:EN=1,AUTOJOIN=1,PEERS=3]', `[WDP:END,count=${rows.filter((r) => r.startsWith('[WDP:N=')).length}]`].join('\n');
const SELF = [
  `[WDP:N=1,CLIENT=0,ALIAS=Body,HW=1,HWREV=,FW=${FW},CAP=0091,CTRL=20,CAPTAGS=,MAESTRO=1,AGE=0,SEEN=1,PEER=3]`,
  '[WDPIF:N=1,S=1,DEV=Maestro 1]',
  '[WDPDA:N=1,S=3,TYPE=HILDA1,FW=9.8.7,HW=,CAPS=,SEEN=1,AGE=12]',
  '[WDPX:N=1,MB=1@57600,WL=-]',
  '[WDPSEQ:N=1,HASH=A1B2C3D4]',
  '[WDPPWM:N=1,DST=2,S=4]',
];
const W2 = [
  `[WDP:N=2,CLIENT=0,ALIAS=Dome,HW=24,HWREV=,FW=${FW},CAP=0011,CTRL=20,CAPTAGS=,MAESTRO=2,AGE=3,SEEN=1,PEER=1]`,
  '[WDPIF:N=2,S=1,DEV=Maestro 2]',
  '[WDPDA:N=2,S=3,TYPE=HILDA2,FW=1.0,HW=revP,CAPS=hil.p,SEEN=0,AGE=-]',
  '[WDPX:N=2,MB=2@115200,WL=1@115200]',
];
const W5 = [`[WDP:N=5,CLIENT=0,ALIAS=Periscope,HW=31,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=7,SEEN=1,PEER=2]`];
const PROBE15 = ['[WDP:N=15,CLIENT=1,ALIAS=HILProbe,HW=0,HWREV=revA,FW=1.0,CAP=0000,CTRL=0,CAPTAGS=hil,MAESTRO=-,AGE=2,SEEN=1,PEER=4]'];
const NAVI20 = ['[WDP:N=20,CLIENT=1,ALIAS=NaviCore,HW=0,HWREV=,FW=2.0,CAP=0040,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=1,SEEN=1,PEER=0]'];
const DUMP = dumpFrom([...SELF, ...W2, ...W5, ...PROBE15, ...NAVI20]);

test('wizard.app_fake_controller the Controller selector stashes each controller\'s board roles and puts them back on the way back, writes the controller only into WCB slots, and follows the NaviCore id', async ({ page }) => {
  await openWizard(page);
  // WCB1 a local Kyber whose target is WCB2's Maestro (so autoComputeKyberTargets agrees with it), WCB2 remote with
  // that Maestro, and a client in slot 3.
  await pullFake(page, 1, board(['BAUD,S2,115200', 'LABEL,S2,Kyber Maestro', 'LABEL,S3,Kyber Marcuino', 'BCAST,OUT,S2,OFF',
                                 'BCAST,IN,S2,OFF', 'KYBER,LOCAL,S2,M2:W2S1:57600'], { wcbq: 3 }));
  await pullFake(page, 2, board(['BAUD,S1,57600', 'MAESTRO,REMOTE', 'MAESTRO,M2:W2S1:57600'], { wcb: 2, wcbq: 3 }));
  const roles = () => page.evaluate(() => [1, 2, 3].map((n) => ({
    mode: boardConfigs[n].kyber.mode, port: boardConfigs[n].kyber.port ?? null, marc: boardConfigs[n].kyber.marcduinoPort ?? null,
    peer: !!boardConfigs[n].specialPeer, id: boardConfigs[n].specialPeerId,
    labels: boardConfigs[n].serialPorts.map((p) => p.label).join('|'),
  })));
  await page.evaluate(() => {
    addDiscoveredBoards([3]);
    boardConfigs[3].type = 'client';
    boardConfigs[3].clientAlias = 'Sensor';
    updateSlotTypeUI(3);
  });
  expect(await page.evaluate(() => systemConfig.general.controller), 'derived on load').toBe('kyber');
  const start = await roles();

  await page.locator('#g-controller-seg [data-controller="navicore"]').click();
  const navi = await roles();
  expect(navi.slice(0, 2).map((r) => [r.mode, r.peer, r.id])).toEqual([['none', true, 20], ['none', true, 20]]);
  expect(navi[0].labels, 'WCB1\'s orphaned Kyber labels cleared').toBe('||||');
  expect(navi[2], 'the client slot is never given the controller').toEqual(start[2]);
  const build = (n) => page.evaluate((n) => WCBParser.buildCommandString(boardConfigs[n], boardBaselines[n]).split('^'), n);
  expect(await build(1)).toContain('?CONTROLLER,ON,20');
  expect(await build(2)).toContain('?CONTROLLER,ON,20');

  await page.locator('#g-controller-seg [data-controller="kyber"]').click();
  expect(await roles(), 'back to Kyber: every role as it was pulled').toEqual(start);
  for (const n of [1, 2]) {
    expect.soft((await push(page, n, { skipReboot: true })).outcome.reason, `WCB${n} left dirty by the flips`).toBe('no changes to push');
  }

  await page.locator('#g-controller-seg [data-controller="navicore"]').click();
  expect(await roles(), 'NaviCore again: its stashed roles come back').toEqual(navi);
  await edit(page, '#g-navicore-id', '17');
  const after = await roles();
  expect(after.map((r) => r.id)).toEqual([17, 17, start[2].id]);
  expect(await build(1)).toContain('?CONTROLLER,ON,17');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_etm_listener a relay\'s ONLINE edge pulls the board and re-arms its terminal, repeated OFFLINE edges start one verify pull, and during an OTA the edges are held for later', async ({ page }) => {
  const W2C = chain(board([], { wcb: 2 }));
  const feed = (line) => page.evaluate((l) => boardConnections[1]._handleLine(l), line);
  const sends = () => page.evaluate(() => __fake.sent(1));

  await openWizard(page);
  await relayFake(page, 1, { 2: W2C });
  await page.evaluate(() => { __fake.log.length = 0; installEtmListener(1); });   // idempotent: setRemoteConnected installed it
  await feed('[ETM] WCB2 came ONLINE (boot) (src MAC: 02:00:00:00:00:02)');
  await page.waitForTimeout(1200);
  let s = await sends();
  expect(s.filter((x) => /MGMT,PULL,2,P$/.test(x)).length, 'a pull').toBe(1);
  expect(s.filter((x) => x.endsWith('?RTERM,START,1')).length, 'the terminal re-armed (3x)').toBe(3);
  // (The listener also toggles a #b<n>-dot, which the board template no longer has: only the badge shows the edge.)
  expect(await page.locator('#b2-status-badge').textContent()).toContain('Remote');

  await openWizard(page);
  await relayFake(page, 1, { 2: W2C });
  await page.evaluate(() => { __fake.log.length = 0; });
  await feed('[ETM] WCB2 went OFFLINE (timeout)');
  await feed('[ETM] WCB2 went OFFLINE (timeout)');
  await page.waitForTimeout(800);
  s = await sends();
  expect(s.filter((x) => /MGMT,PULL,2,P$/.test(x)).length, 'one verify pull for two edges').toBe(1);
  expect(await page.locator('#b2-status-badge').textContent()).toContain('Retrying');

  // An OTA owns the relay link: the edges are recorded, per board, and nothing is sent.
  await openWizard(page);
  await relayFake(page, 1, { 2: W2C, 3: chain(board([], { wcb: 3 })) });
  await page.evaluate(() => { __fake.log.length = 0; _otaInProgress.add(9); });
  await feed('[ETM] WCB2 came ONLINE (boot)');
  await feed('[ETM] WCB3 went OFFLINE (timeout)');
  await page.waitForTimeout(800);
  expect(await sends()).toEqual([]);
  // A Set of BOARDS (app.js:93), so the plan's "2 edges" is two boards here: one edge each.
  expect(await page.evaluate(() => [..._suppressedEtmEdges].sort())).toEqual([2, 3]);
  await page.evaluate(() => { _otaInProgress.clear(); _suppressedEtmEdges.clear(); });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_wdp_panel parseWdpDump reads every firmware line - WDP with PEER, WDPIF, WDPDA with SEEN and AGE (AGE=- too), WDPX, WDPPWM, WDPCFG with EN - the panel shows a quiet device as not heard, and Forget, Clear, AutoJoin and Poll send their commands', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, 1, board(), { replies: { '?WDP,DUMP': DUMP } });
  const parsed = await page.evaluate((d) => parseWdpDump(d), DUMP);
  const byN = Object.fromEntries(parsed.nodes.map((n) => [n.n, n]));
  expect(parsed.nodes.map((n) => [n.n, n.peer, n.client, n.live])).toEqual(
    [[1, 3, false, true], [2, 1, false, true], [5, 2, false, true], [15, 4, true, true], [20, 0, true, true]]);
  expect(byN[1]).toMatchObject({ alias: 'Body', hw: 1, fw: FW, cap: 0x91, ctrl: 20, maestro: '1', mb: '1@57600', wl: '-',
                                 pwm: [{ dst: 2, s: 4 }], ifs: [{ s: 1, dev: 'Maestro 1' }] });
  expect(byN[1].da).toEqual([{ s: 3, type: 'HILDA1', fw: '9.8.7', hw: '', caps: '', seen: true }]);
  expect(byN[2].da).toEqual([{ s: 3, type: 'HILDA2', fw: '1.0', hw: 'revP', caps: 'hil.p', seen: false }]);
  expect([byN[2].mb, byN[2].wl, byN[15].hwRev, byN[15].capTags]).toEqual(['2@115200', '1@115200', 'revA', 'hil']);
  expect(parsed.cfg).toEqual({ enabled: true, autojoin: true, peers: 3 });
  // A dump from firmware before PEER=, SEEN on WDPDA and EN= still parses.
  const old = await page.evaluate(() => parseWdpDump([
    '[WDP:N=2,CLIENT=0,ALIAS=Old,HW=24,HWREV=,FW=6.0,CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=1,SEEN=1]',
    '[WDPDA:N=2,S=4,TYPE=OLD,FW=1,HW=,CAPS=]', '[WDPCFG:AUTOJOIN=0,PEERS=1]'].join('\n')));
  expect(old).toEqual({ nodes: [expect.objectContaining({ n: 2, peer: null, da: [expect.objectContaining({ type: 'OLD', seen: true })] })],
                        cfg: { enabled: true, autojoin: false, peers: 1 } });

  await page.evaluate(() => { __fake.meshOn(); return wdpMeshRefresh(); });
  const body = page.locator('#wdp-mesh-body');
  expect(await body.locator('tbody tr').count()).toBe(5);
  expect(await body.locator('.wdp-da.wdp-quiet').textContent()).toContain('(not heard)');
  expect(await body.textContent()).toContain('PWM → WCB2 S4');
  expect(await body.locator('.wdp-peer-learned').allTextContents()).toEqual(['auto-joined', 'temporary']);

  const run = async (fn) => {
    await page.evaluate(() => { __fake.log.length = 0; });
    await page.evaluate(fn);
    await page.waitForTimeout(400);
    return page.evaluate(() => __fake.sent(1));
  };
  // Forget a device on another board: a FRAG through the board the panel talks to, in the target's function identifier.
  let s = await run(() => document.querySelector('.wdp-btn-forget[data-type="HILDA2"]').click());
  expect(s.filter((x) => x.includes('WDP,DA')).map((x) => x.replace(/^(\?MGMT,FRAG,2,)[0-9A-F]{4}(,0,1,)/, '$1SID$2')))
    .toEqual(['?MGMT,FRAG,2,SID,0,1,?WDP,DA,FORGET,S3,HILDA2']);
  s = await run(() => document.querySelector('.wdp-btn-forget[data-type="HILDA1"]').click());
  expect(s).toContain('?WDP,DA,FORGET,S3,HILDA1');
  expect(await run(() => wdpForgetPeer(5))).toContain('?WDP,FORGET,5');
  expect(await run(() => wdpClearLearned())).toContain('?WDP,CLEAR');
  expect(await run(() => document.querySelector('.wdp-toolbar .wdp-btn').click())).toContain('?WDP,AUTOJOIN,OFF');
  expect(await run(() => wdpPollMesh())).toContain('?WDP,POLL');

  // WDP turned off on the board: a definite state, said as such.
  await page.evaluate(() => renderWdpMesh([], 1, { enabled: false, autojoin: true, peers: 0 }));
  expect(await body.textContent()).toContain('WDP discovery is disabled on WCB 1');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_mesh_tick the 12 s discovery tick adds unknown WCBs as sections with their advertised alias and no pull, gives clients a card, drops a temporary client that goes quiet and marks others Offline, and skips while a push, flash, OTA or stats capture owns the port', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, 1, board(), { replies: { '?WDP,DUMP': DUMP } });
  await page.evaluate(() => { __fake.meshOn(); __fake.log.length = 0; return meshAutoDiscoverTick(); });
  const state = () => page.evaluate(() => ({
    sections: [...document.querySelectorAll('[id^="section-board-"]')].map((e) => +e.id.slice(14)).sort((a, b) => a - b),
    alias: [boardConfigs[2]?.alias, boardConfigs[5]?.alias],
    clients: [15, 20].map((n) => [boardConfigs[n]?.type, boardConfigs[n]?.clientAlias,
                                  document.getElementById(`b${n}-client-status`)?.textContent.includes('Online')]),
    remote: Object.keys(remoteRelayForBoard),
  }));
  expect(await state()).toEqual({ sections: [1, 2, 5, 15, 20], alias: ['Dome', 'Periscope'],
                                  clients: [['client', 'HILProbe', true], ['client', 'NaviCore', true]], remote: [] });
  expect(await page.evaluate(() => __fake.sent(1)), 'discovery sends nothing but the dump').toEqual([]);
  expect(await page.evaluate(() => __fake.log.filter((e) => e.op === 'collect').map((e) => e.s))).toEqual(['?WDP,DUMP']);

  // Next sweep without either client: the temporary one above the floor vanishes, NaviCore stays as Offline.
  await page.evaluate((d) => { boardConnections[1].__replies['?WDP,DUMP'] = d; return meshAutoDiscoverTick(); },
                      dumpFrom([...SELF, ...W2, ...W5]));
  const s2 = await state();
  expect(s2.sections).toEqual([1, 2, 5, 20]);
  expect(await page.locator('#b20-client-status').textContent()).toContain('Offline');

  // The tick keeps out of a stream something else is reading.
  for (const [what, on, off] of [
    ['a push', () => _pushingBoards.add(1), () => _pushingBoards.clear()],
    ['a flash', () => { _boardFlashing[1] = true; }, () => { delete _boardFlashing[1]; }],
    ['an OTA', () => _otaInProgress.add(1), () => _otaInProgress.clear()],
    ['a stats capture', () => { _statsFetchBusy = true; }, () => { _statsFetchBusy = false; }],
  ]) {
    await page.evaluate(`(${on.toString()})()`);
    await page.evaluate(() => { __fake.log.length = 0; return meshAutoDiscoverTick(); });
    expect.soft(await page.evaluate(() => __fake.log.length), what).toBe(0);
    await page.evaluate(`(${off.toString()})()`);
  }
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_partial_ack ACK pacing retries a command once; one with no answer after the retry fails the push (the rest still sent), which then pulls to see what landed; an unconnected board is refused; a remote PWM destination board is pulled after the push', async ({ page }) => {
  await openWizard(page);
  const dead = [{ ok: false, lines: [] }, { ok: false, lines: [] }];
  await pullFake(page, 1, board(), { acks: { '?LABEL,S1,HILA': dead } });
  await edit(page, '#b1-s1-label', 'HILA');
  await edit(page, '#b1-s2-label', 'HILB');
  await page.evaluate(() => { __fake.log.length = 0; });
  const r = await push(page, 1);
  expect(r.sent).toEqual(['1:?LABEL,S1,HILA', '1:?LABEL,S1,HILA', '1:?LABEL,S2,HILB']);
  expect(r.outcome).toEqual({ ok: false, aborted: false, reason: 'some settings got no response after retry' });
  expect(await page.evaluate(() => __fake.toastText())).toContain('some settings may not have applied');
  await page.waitForTimeout(700);
  expect(await page.evaluate(() => __fake.log.filter((e) => e.op === 'collect').map((e) => e.s)), 'the verify pull')
    .toContain('WCB_WEBTOOL_CONFIG_PULL');

  // Not connected: refused, nothing sent.
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  const nc = await push(page, 1);
  expect([nc.sent, nc.outcome.reason]).toEqual([[], 'board not connected']);
  expect(await page.evaluate(() => __fake.toastText())).toContain('Board not connected');

  // A PWM output on WCB2: WCB2 (a USB board too) is pulled 2.5 s after WCB1's push.
  await openWizard(page);
  await pullFake(page, 1, board([], { wcbq: 2 }));
  await pullFake(page, 2, board([], { wcb: 2 }));
  await page.evaluate(() => {
    boardConfigs[1].mappings.push({ type: 'PWM', sourcePort: 5, rawMode: false, destinations: [{ wcbNumber: 2, port: 3 }] });
    populateMappingsFromConfig(1, boardConfigs[1]);
    __fake.log.length = 0;
  });
  const p = await push(page, 1, { skipReboot: true });
  expect(p.sent).toEqual(['1:?MAP,PWM,S5,W2S3']);
  await page.waitForTimeout(3000);
  expect(await page.evaluate(() => __fake.log.filter((e) => e.slot === 2 && e.op === 'collect').map((e) => e.s)))
    .toContain('WCB_WEBTOOL_CONFIG_PULL');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_serial_claims a port a device has claimed shows its broadcast boxes cleared but never pushes them, while an unclaimed port\'s box change is pushed', async ({ page }) => {
  await openWizard(page);
  // MP3 on S2 with both broadcast flags ON on the board.
  await pullFake(page, 1, board(['BAUD,S2,38400', 'MP3,S2:38400:V3']));
  expect(await page.evaluate(() => [document.getElementById('b1-s2-bcin').checked, document.getElementById('b1-s2-bcout').checked,
                                    boardConfigs[1].serialPorts[1].broadcastIn, boardConfigs[1].serialPorts[1].broadcastOut]))
    .toEqual([false, false, true, true]);
  expect((await push(page, 1)).outcome.reason).toBe('no changes to push');
  await edit(page, '#b1-s4-bcout', false);
  expect((await push(page, 1)).sent).toEqual(['1:?BCAST,OUT,S4,OFF']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_terminal the debug toggles send ?DEBUG,<mode>,ON/OFF per board (through the relay for a remote one) and reset on disconnect; the terminal hides discovery dumps, telemetry and config-tool traffic but shows ordinary lines, ?TERMDEBUG reveals them, a hand-typed ?WDP,DUMP shows its rows; timestamps and pane visibility work', async ({ page }) => {
  await openWizard(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await page.evaluate(() => { __fake.log.length = 0; });
  const sends = () => page.evaluate(() => __fake.sent(1));
  await page.evaluate(async () => { await toggleDebug(1, 'etm'); await toggleDebug(1, 'main'); await toggleDebug(1, 'etm');
                                    await toggleDebug(1, 'mgmt'); await toggleDebug(2, 'maestro'); });
  const s = await sends();
  expect(s.slice(0, 4)).toEqual(['?DEBUG,ETM,ON', '?DEBUG,ON', '?DEBUG,ETM,OFF', '?DEBUG,MGMT,ON']);
  expect(s[4].replace(/^(\?MGMT,FRAG,2,)[0-9A-F]{4}/, '$1SID')).toBe('?MGMT,FRAG,2,SID,0,1,?DEBUG,MAESTRO,ON');
  expect(await page.evaluate(() => [boardDebugStates[1], document.getElementById('dbg-btn-1-mgmt').classList.contains('debug-on')]))
    .toEqual([{ main: true, maestro: false, pwm: false, hcr: false, etm: false, mgmt: true }, true]);
  await page.evaluate(() => updateTerminalPaneDot(1, false));
  expect(await page.evaluate(() => Object.values(boardDebugStates[1]).some(Boolean)), 'a disconnect clears them').toBe(false);
  await page.evaluate(() => updateTerminalPaneDot(1, true));

  const hidden = [
    '[WDP:N=2,CLIENT=0,ALIAS=Dome,HW=24,HWREV=,FW=6.2,CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=1,SEEN=1,PEER=1]',
    '[WDPCFG:EN=1,AUTOJOIN=1,PEERS=2]', 'Processing input from USB: ?WDP,DUMP',
    '{"type":"rc_trig","id":7}', '{"f":1,"of":3,"sid":5,"s":"abc"}', '{"bb":1,"n":3}',
    'Processing ETM input from WCB20: {"type":"GET_CONFIG"}', '{"type":"PONG","sys":1}',
  ];
  const shown = ['Ordinary board output HIL1', '[WDP] learned WCB5 Periscope'];
  const pane = () => page.evaluate(() => document.getElementById('term-pane-output-1').textContent);
  await page.evaluate((lines) => { clearTerminalPane(1); for (const l of lines) boardConnections[1]._handleLine(l); }, [...hidden, ...shown]);
  let text = await pane();
  for (const l of hidden) expect.soft(text, `hidden: ${l}`).not.toContain(l);
  for (const l of shown) expect.soft(text, `shown: ${l}`).toContain(l);
  // rc_hb / rc_ch are dropped before the display even under ?TERMDEBUG unless it is on.
  await page.evaluate(() => { document.getElementById('term-pane-input-1').value = '?TERMDEBUG,ON'; return sendTerminalCommandTo(1); });
  await page.evaluate((lines) => { clearTerminalPane(1); for (const l of lines) boardConnections[1]._handleLine(l); },
                      [...hidden, '{"type":"rc_hb","id":7}']);
  text = await pane();
  for (const l of [...hidden, '{"type":"rc_hb","id":7}']) expect.soft(text, `?TERMDEBUG,ON shows: ${l}`).toContain(l);
  await page.evaluate(() => { document.getElementById('term-pane-input-1').value = '?TERMDEBUG,OFF'; return sendTerminalCommandTo(1); });
  expect(await sends(), '?TERMDEBUG never reaches the board').not.toContain('?TERMDEBUG,ON');

  // A hand-typed ?WDP,DUMP opens a window in which its rows show, until [WDP:END.
  await page.evaluate(async (row) => {
    clearTerminalPane(1);
    document.getElementById('term-pane-input-1').value = '?WDP,DUMP';
    await sendTerminalCommandTo(1);
    for (const l of [row, '[WDP:END,count=1]', row]) boardConnections[1]._handleLine(l);
  }, hidden[0]);
  text = await pane();
  expect(text.split(hidden[0]).length - 1, 'shown once: the row after [WDP:END is hidden again').toBe(1);

  // Timestamps, auto-scroll, pane visibility.
  await page.evaluate(() => { clearTerminalPane(1); toggleTimestamp(1); termLog(1, 'stamped', 'out'); toggleTimestamp(1); });
  expect(await pane()).toMatch(/^\[\d\d:\d\d:\d\d\] stamped$/);
  expect(await page.evaluate(() => { toggleAutoScroll(1); const a = boardAutoScroll[1]; toggleAutoScroll(1); return [a, boardAutoScroll[1]]; }))
    .toEqual([false, true]);
  await page.evaluate(() => { document.getElementById('term-vis-cb-1').checked = false; togglePaneVisibility(1); });
  expect(await page.evaluate(() => document.getElementById('term-pane-1').style.display)).toBe('none');
  expect(page.wizErrors).toEqual([]);
});

// What exportSystemFile / wizardExportConfig would download: the Blob handed to URL.createObjectURL, with the anchor
// click that starts a download suppressed. Returns null when nothing was exported.
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
const undated = (text) => text.split('\n').filter((l) => !l.startsWith('# Created:')).join('\n');

test('wizard.app_fake_setup_wizard the guided setup validates its steps (unique ids, the controller\'s reserved id, a hardware version per WCB, no Maestro on the Kyber\'s ports), applies Kyber local with every Maestro as a target and the other Maestro board as remote, and its export parses back to the same boards', async ({ page }) => {
  await openWizard(page);
  await install(page);
  await page.evaluate(() => {
    openWizard();                                       // the page's own setup wizard (not lib/wizard.js's)
    const b = (n) => { const x = wizardDefaultBoard(n); x.hwVersion = 24; x.serialPorts[0].baud = 57600; return x; };
    Object.assign(wizardState, {
      quantity: 2, password: 'dummy_wizard_password', mac2: '05', mac3: '4B', boards: [b(1), b(2)],
      controlSystem: 'kyber', kyberEnabled: true, kyberBoard: 1, kyberPort: 2, kyberBaud: 115200, kyberMarcduinoPort: 3,
      maestroEnabled: true,
      maestros: [{ boardSlot: 1, id: 1, port: 1, baud: 57600 }, { boardSlot: 2, id: 2, port: 1, baud: 57600 }],
    });
    wizardState.steps = buildWizardSteps();
  });
  // Validation, on the steps' own rendered inputs.
  const check = (step, set) => page.evaluate(({ step, set }) => {
    wizardState.currentIdx = wizardState.steps.indexOf(step);
    wizardRenderStep();
    for (const [id, v] of Object.entries(set)) document.getElementById(id).value = v;
    return wizardValidateStep(step);
  }, { step, set });
  expect(await check('identity', { 'wiz-b0-wcbnum': '1', 'wiz-b1-wcbnum': '1' })).toMatch(/unique ID/);
  await page.evaluate(() => { wizardState.useSpecialPeer = true; wizardState.navicoreId = 20; });
  expect(await check('identity', { 'wiz-b0-wcbnum': '1', 'wiz-b1-wcbnum': '20' })).toMatch(/ID 20 is reserved/);
  await page.evaluate(() => { wizardState.useSpecialPeer = false; });
  expect(await check('identity', { 'wiz-b0-wcbnum': '1', 'wiz-b1-wcbnum': '2', 'wiz-b1-hwver': '0' })).toMatch(/hardware version/);
  expect(await check('identity', { 'wiz-b0-wcbnum': '1', 'wiz-b1-wcbnum': '2', 'wiz-b1-hwver': '24' })).toBe(null);
  expect(await check('maestro-config', { 'wiz-m0-port': '2' })).toMatch(/claimed by Kyber's Maestro port/);
  expect(await check('maestro-config', { 'wiz-m0-port': '3' })).toMatch(/Kyber's Marcduino port/);
  expect(await check('maestro-config', { 'wiz-m0-port': '1' })).toBe(null);

  const r = await page.evaluate(() => {
    wizardApplyConfig();
    const pick = (n) => ({ kyber: boardConfigs[n].kyber, maestros: boardConfigs[n].maestros, table: boardConfigs[n].maestroTable,
                           ports: boardConfigs[n].serialPorts.map((p) => [p.baud, p.label, p.broadcastIn, p.broadcastOut]) });
    return { b1: pick(1), b2: pick(2), controller: systemConfig.general.controller,
             build: [1, 2].map((n) => WCBParser.buildCommandString(boardConfigs[n], null, true).split('^')) };
  });
  const TABLE = [{ id: 1, wcb: 1, port: 1, baud: 57600 }, { id: 2, wcb: 2, port: 1, baud: 57600 }];
  expect(r.b1.kyber).toEqual({ mode: 'local', port: 2, baud: 115200, marcduinoPort: 3, targets: TABLE });
  expect(r.b1.ports.slice(0, 3)).toEqual([[57600, '', true, true], [115200, 'Kyber Maestro', true, true], [9600, 'Kyber Marcuino', true, true]]);
  expect([r.b1.maestros, r.b1.table]).toEqual([[{ id: 1, port: 1, baud: 57600 }], TABLE]);
  expect([r.b2.kyber.mode, r.b2.maestros, r.controller]).toEqual(['remote', [{ id: 2, port: 1, baud: 57600 }], 'kyber']);
  expect(r.build[0]).toContain('?KYBER,LOCAL,S2,M1:W1S1:57600,M2:W2S1:57600');
  expect(r.build[0]).toContain('?MAESTRO,M1:W1S1:57600,M2:W2S1:57600');
  expect(r.build[1]).toEqual(expect.arrayContaining(['?MAESTRO,REMOTE', '?MAESTRO,M1:W1S1:57600,M2:W2S1:57600']));

  // The export, read back: the same boards (Kyber targets on a non-Kyber board are derived from its Maestro table,
  // and the push re-derives them, so they are left out, as parser.test.js does).
  const text = await captureExport(page, () => wizardExportConfig());
  expect(text).toBeTruthy();
  const back = await page.evaluate((text) => {
    const sys = WCBParser.parseSystemFile(text);
    // Targets compared as a set: the file lists the ?MAESTRO table before ?KYBER,LOCAL (the builder claims the Kyber
    // port late), so a parse meets WCB2's Maestro first. What that order costs a later push is W-20.
    const key = (t) => `M${t.id}:W${t.wcb}S${t.port}:${t.baud}`;
    const view = (c) => ({ n: c.wcbNumber, hw: c.hwVersion, mode: c.kyber.mode, port: c.kyber.port ?? null,
                           targets: c.kyber.mode === 'local' ? c.kyber.targets.map(key).sort() : null, maestros: c.maestros,
                           ports: c.serialPorts.map((p) => [p.baud, p.label]), etm: c.etm.enabled });
    return { file: sys.boards.map(view), page: [1, 2].map((n) => view(boardConfigs[n])), qty: sys.general.wcbQuantity };
  }, text);
  expect(back.file).toEqual(back.page);
  expect(back.qty).toBe(2);
  expect(page.wizErrors).toEqual([]);
});

// Four slots for the system file: WCB1 and WCB2 (the floor), WCB5 (above the floor, a board WDP joined) with labels,
// a sequence and a variable, and client 20.
async function fourSlots(page) {
  await openWizard(page);
  await pullFake(page, 1, board(['LABEL,S1,Body Maestro'], { wcbq: 2 }));
  await pullFake(page, 2, board(['LABEL,S2,Dome Panel'], { wcb: 2 }));
  await pullFake(page, 5, board(['LABEL,S3,Periscope', 'SEQ,SAVE,up,;S3UP', 'VAR,SET,hilv,7'], { wcb: 5 }));
  await page.evaluate(() => {
    addDiscoveredBoards([20]);
    boardConfigs[20].type = 'client';
    boardConfigs[20].clientAlias = 'NaviCore';
    updateSlotTypeUI(20);
  });
}

test('wizard.app_fake_system_file the export writes every board the Wizard knows - the floor, a board above it with its labels, sequence and variable, a client slot with its token - under the General WCB quantity, and refuses two boards on one number', async ({ page }) => {
  await fourSlots(page);
  const text = await captureExport(page, () => exportSystemFile());
  const sections = text.split('\n').filter((l) => /^\[.*\]$/.test(l));
  expect(sections).toEqual(['[GENERAL]', '[WCB1]', '[WCB2]', '[WCB5]', '[WCB20]']);
  const body = (name) => text.split('\n')[text.split('\n').indexOf(name) + 1];
  expect(body('[GENERAL]').split('^')).toContain('?WCBQ,2');
  expect(body('[WCB5]').split('^')).toEqual(expect.arrayContaining(['?LABEL,S3,Periscope', '?SEQ,SAVE,up,;S3UP', '?VAR,SET,hilv,7']));
  expect(body('[WCB20]').endsWith('^?CLIENT,NaviCore')).toBe(true);

  // Two slots on one number would write two [WCB2] blocks and the reload would keep one: refused, nothing written.
  await page.evaluate(() => { boardConfigs[5].wcbNumber = 2; });
  expect(await captureExport(page, () => exportSystemFile())).toBe(null);
  expect(await page.evaluate(() => __fake.toastText())).toContain('Export aborted — two boards share the same number');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_system_file_reload (should) a saved system file loads back as it was saved: the WCB quantity it carries, no board it did not hold, and a board above the floor with its labels, sequences and variables, so saving it again writes the same file (W-17)', async ({ page }) => {
  test.fail(true, 'W-17: parseSystemFile raises the WCB quantity to the number of [WCB] sections (parser.js:1257-1260), client slots and ' +
                  'boards above the floor included; loadSystemFileContent then renders sections for 1..that number only (app.js:9270), ' +
                  'adding default boards that were never in the file, and none for a board above it - which the next export writes ' +
                  'from its missing DOM, with no labels, sequences or variables (app.js:9314-9327)');
  await fourSlots(page);
  const first = await captureExport(page, () => exportSystemFile());
  await openWizard(page);                  // a fresh page, as when the file is opened another day
  await install(page);
  await page.evaluate((t) => loadSystemFileContent(t), first);
  expect(await page.evaluate(() => [document.getElementById('g-wcbq').value, systemConfig.general.wcbQuantity])).toEqual(['2', 2]);
  expect(await page.evaluate(() => Object.keys(boardConfigs).map(Number).sort((a, b) => a - b))).toEqual([1, 2, 5, 20]);
  const second = await captureExport(page, () => exportSystemFile());
  expect(undated(second)).toBe(undated(first));
});

test('wizard.app_fake_identity the identity fields: the LED pin shows for 3.x boards, takes a preset or a custom 0-48, a 3.1 board loads clean, a new WCB number pushes ?WCB with a reboot, the alias is cut at 24 characters and cleaned like the firmware does, and a client slot has no Push button and is never pushed', async ({ page }) => {
  await openWizard(page);
  await pullFake(page, 1, board(['HW,32', 'LED,PIN,47']));
  expect(await page.locator('#b1-led-pin-group').isVisible()).toBe(true);
  await edit(page, '#b1-led-pin', '0');
  await edit(page, '#b1-led-pin-custom', '49');          // out of range: not taken
  expect((await push(page, 1)).outcome.reason).toBe('no changes to push');
  await edit(page, '#b1-led-pin-custom', '21');
  expect((await push(page, 1)).sent).toEqual(['1:?LED,PIN,21']);

  // A 3.1 board loaded: the board itself has nothing pending (its "Changes pending — push to WCB 1" toast would say so).
  // General's own toasts on a first pull are W-19, pinned by wizard.app_fake_pull_leaves_nothing_pending.
  await openWizard(page);
  await pullFake(page, 1, board(['HW,31']));
  expect(await page.locator('#b1-unsaved-badge').isVisible()).toBe(false);
  expect(await page.evaluate(() => __fake.toastText())).not.toContain('push to WCB 1');
  expect(await page.locator('#b1-led-pin-group').isVisible()).toBe(true);

  // A new number, within the quantity: ?WCB,3, which needs a reboot.
  await openWizard(page);
  await pullFake(page, 1, board([], { wcbq: 3 }));
  await edit(page, '#b1-wcb-number', '3');
  const r = await push(page, 1, { skipReboot: true });
  expect([r.sent, r.returned]).toEqual([['1:?WCB,3'], true]);

  // The alias: 24 characters, and ^ , ; ? replaced with _ as saveWCBAlias does.
  await openWizard(page);
  await pullFake(page, 1, board());
  await edit(page, '#b1-alias', 'Dome^Main,Body;Left?' + 'X'.repeat(20));
  expect(await page.evaluate(() => boardConfigs[1].alias)).toBe('Dome_Main_Body_Left_XXXX');

  // A client slot: its WCB body (and Push button) is hidden, and Push All passes it by.
  await page.evaluate(() => {
    addDiscoveredBoards([3]);
    boardConfigs[3].type = 'client';
    updateSlotTypeUI(3);
  });
  expect(await page.evaluate(() => [document.getElementById('b3-section-body').style.display,
                                    document.getElementById('b3-client-pane').style.display])).toEqual(['none', '']);
  expect(await page.locator('#b3-btn-go').isVisible()).toBe(false);
  await page.evaluate(async () => { __fake.log.length = 0; await boardGoAll(); });
  expect(await page.evaluate(() => __fake.log.filter((e) => e.slot === 3).length)).toBe(0);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_wcb_number_above_floor (should) a board above the WCB quantity can be renumbered to any number its dropdown offers (W-18)', async ({ page }) => {
  test.fail(true, 'W-18: populateUIFromConfig offers numbers up to the board\'s own (app.js:3196-3202), but onWCBNumberChange takes ' +
                  'only numbers up to the General quantity (app.js:2080-2081): WCB5 on a WCBQ 2 mesh shows 4 picked while the ' +
                  'config keeps 5, and the push sends nothing');
  await openWizard(page);
  await pullFake(page, 1, board([], { wcb: 5, wcbq: 2 }));       // migrates to slot 5
  expect(await page.evaluate(() => [...document.getElementById('b5-wcb-number').options].map((o) => o.value)))
    .toEqual(['1', '2', '3', '4', '5']);
  await edit(page, '#b5-wcb-number', '4');
  expect(await page.evaluate(() => boardConfigs[5].wcbNumber)).toBe(4);
  expect((await push(page, 5, { skipReboot: true })).sent).toEqual(['5:?WCB,4']);
});

test('wizard.app_fake_fw_check the latest-release check reads the version from the bin name on GitHub, a board behind it shows the update badge, one on it shows up to date, a newer one shows dev, a malformed one shows a mismatch without breaking the page, the check is throttled, and a stale branch override warns', async ({ page }) => {
  const asked = [];
  let fail = false;
  await page.route('https://api.github.com/**', (route) => {
    asked.push(route.request().url());
    if (fail) return route.fulfill({ status: 500, body: '' });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([
      { type: 'file', name: 'WCB_S3_custom_bootloader_16MB_wdt3s.bin', download_url: 'https://fake.invalid/a' },
      { type: 'file', name: 'WCB_6.2.3_010101ROCT2026_WIFI_ESP32.bin', download_url: 'https://fake.invalid/b' },
      { type: 'file', name: 'WCB_6.2.3_010101ROCT2026_WIFI_ESP32S3.bin', download_url: 'https://fake.invalid/c' },
    ]) });
  });
  await openWizard(page);
  await install(page);
  await page.waitForFunction(() => latestFirmwareVersion === 'v6.2.3_010101ROCT2026', null, { timeout: 10_000 });
  expect(asked.some((u) => u.includes('/contents/Code/bin?ref=main'))).toBe(true);
  const show = (ver) => page.evaluate((ver) => {
    boardConfigs[1].fwVersion = ver;
    updateBoardSwVersionDisplay(1);
    return [document.getElementById('b1-sw-version').textContent, document.getElementById('b1-btn-update-fw').style.display];
  }, ver);
  expect(await show('6.2.1_290236RSEP2026')).toEqual(['v6.2.1_290236RSEP2026 ↑', '']);
  expect(await show('6.2.3_010101ROCT2026')).toEqual(['v6.2.3_010101ROCT2026 ✓', 'none']);
  expect(await show('6.2.4_020101ROCT2026')).toEqual(['v6.2.4_020101ROCT2026 (dev)', 'none']);
  expect(await show('garbage')).toEqual(['vgarbage ≠', '']);
  expect(await page.evaluate(() => [parseWCBVersion('v6.0_031250RMAR2026')?.toISOString?.() ? 'date' : null,
                                    parseWCBVersion('6.2'), parseWCBVersion('6.2.1_290236RXYZ2026')])).toEqual(['date', null, null]);
  expect(await page.evaluate(() => {
    const d = parseWCBVersion('6.2.1_290236RSEP2026');
    return [d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes()];
  })).toEqual([2026, 8, 29, 2, 36]);

  // GitHub unreachable: the check says so and the board is shown as installed.
  fail = true;
  expect(await page.evaluate(async () => { latestFirmwareVersion = null; return fetchLatestFirmwareVersion(); })).toBeFalsy();
  expect((await show('6.2.1_290236RSEP2026'))[0]).toBe('v6.2.1_290236RSEP2026 ✓');
  fail = false;

  // Throttled: a second check inside 5 s only asks the user to wait.
  const n0 = asked.length;
  await page.evaluate(async () => { _fwCheckLastMs = 0; await boardCheckFwUpdate(1); await boardCheckFwUpdate(1); });
  expect(asked.length - n0, 'one request for two clicks').toBe(1);
  expect(await page.evaluate(() => __fake.toastText())).toContain('Please wait');

  // A leftover branch override: warned about, and the check follows it.
  await page.evaluate(() => { localStorage.setItem('wcb_fw_branch', 'feature/x'); warnStaleFwBranchOverride(); });
  expect(await page.evaluate(() => __fake.toastText())).toContain("overridden to branch 'feature/x'");
  await page.evaluate(() => fetchLatestFirmwareVersion());
  expect(asked.at(-1)).toContain('?ref=feature/x');
  await page.evaluate(() => localStorage.removeItem('wcb_fw_branch'));
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_relay_card a MgmtRelay gets its card anchored at its number, lists the WCBs its mesh hears (not clients, not itself), Manage all arms each one\'s terminal and pulls them one at a time, a second Manage all only re-arms, Push All and the export leave the relay out, and a disconnect un-manages its boards', async ({ page }) => {
  const P = require('../../../Wizard/parser.js');
  const hex8 = (n) => n.toString(16).toUpperCase().padStart(8, '0');
  const reply = (c) => `[VER:${FW}]${c}^?CHK${hex8(P.crc32(c))}`;
  const RELAY_DUMP = dumpFrom([
    `[WDP:N=19,CLIENT=0,ALIAS=Relay,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=0,SEEN=1,PEER=3]`,
    ...W2, `[WDP:N=3,CLIENT=0,ALIAS=Legs,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=2,SEEN=1,PEER=1]`,
    ...PROBE15]);
  await openWizard(page);
  await pullFake(page, 1, board(['RELAY,1'], { wcb: 19 }), { replies: { '?WDP,DUMP': RELAY_DUMP } });
  await page.evaluate(() => { __fake.meshOn(); return meshAutoDiscoverTick(); });
  const card = page.locator('#relay-card-19');
  expect(await card.textContent()).toContain('Relaying2 board(s)');
  expect(await card.locator('button', { hasText: 'Manage via relay' }).count()).toBe(2);

  // Manage all: both armed at once (RTERM,START x3 each), then pulled one after the other.
  await page.evaluate(() => { __fake.log.length = 0; window.__all = relayRouteAll(19); });
  const pulls = () => page.evaluate(() => __fake.sent(19).filter((s) => /MGMT,PULL,\d+,P$/.test(s)));
  await expect.poll(pulls).toEqual(['?MGMT,PULL,2,P']);
  await page.waitForTimeout(800);            // RTERM,START goes 3 x, 250 ms apart (startRemoteTermSession)
  expect(await page.evaluate(() => __fake.sent(19).filter((s) => s.includes('RTERM,START')).map((s) => s.split(',')[2]).sort()))
    .toEqual(['2', '2', '2', '3', '3', '3']);
  expect(await pulls(), 'WCB3 waits for WCB2\'s reply').toEqual(['?MGMT,PULL,2,P']);
  await page.evaluate((l) => boardConnections[19]._handleLine(l), `[MGMT:CONFIG,2]${reply(chain(board([], { wcb: 2 })))}`);
  await expect.poll(pulls).toEqual(['?MGMT,PULL,2,P', '?MGMT,PULL,3,P']);
  await page.evaluate((l) => boardConnections[19]._handleLine(l), `[MGMT:CONFIG,3]${reply(chain(board([], { wcb: 3 })))}`);
  await page.evaluate(() => window.__all);
  await page.waitForTimeout(800);            // each pull that lands re-arms its terminal again, 3 x 250 ms
  expect(await page.evaluate(() => [remoteRelayForBoard[2], remoteRelayForBoard[3], !!boardBaselines[2], !!boardBaselines[3]]))
    .toEqual([19, 19, true, true]);
  expect(await page.evaluate(() => __fake.toastText())).toContain('Manage all: 2/2 config(s) pulled via WCB19');

  // Again: the stale terminals are re-armed, nothing is pulled.
  await page.evaluate(async () => { __fake.log.length = 0; await relayRouteAll(19); });
  await page.waitForTimeout(800);            // the re-arms are not awaited: 3 x 250 ms each
  expect([await pulls(), await page.evaluate(() => __fake.sent(19).filter((s) => s.includes('RTERM,START')).length)]).toEqual([[], 6]);

  // Push All has nothing for the relay itself, and the export has no [WCB19].
  await page.evaluate(async () => { __fake.log.length = 0; await boardGoAll(); });
  expect(await page.evaluate(() => __fake.sent(19).filter((s) => !s.includes('RTERM')))).toEqual([]);
  const text = await captureExport(page, () => exportSystemFile());
  expect(text).not.toContain('[WCB19]');

  // A disconnect: RTERM,STOP to each managed board, which are no longer managed; the card goes.
  await page.evaluate(async () => { __fake.log.length = 0; await boardDisconnect(19); });
  expect(await page.evaluate(() => __fake.sent(19).filter((s) => s.endsWith('RTERM,STOP')).length)).toBe(2);
  expect(await page.evaluate(() => [Object.keys(remoteRelayForBoard), [..._relaySlots], !!document.getElementById('relay-card-19')]))
    .toEqual([[], [], false]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_hub_flash_refused a flash or an erase on the shared port is refused while this tab only follows the hub, and while another tab waits on the hub\'s lock; nothing is flashed and the push outcome says it did not run', async ({ page }) => {
  // The no-board half of WCB-WP21 row 7 (boardGo's shared-port guard, app.js:7504-7520). The hub protocol itself is
  // serial-hub.js, in lockstep with NaviCore's copy (nctool.static) and run two-page by nctool.shared_hub_two_pages.
  await openWizard(page);
  await pullFake(page, 1, board(), { shared: true });
  await page.evaluate(() => {
    updateConnectionUI(1, true);             // as a real connect leaves it: Push enabled
    window.__flashed = 0;
    window.flashFirmware = async () => { window.__flashed++; };
  });
  const attempt = (mode) => page.evaluate(async (mode) => {
    const before = __fake.toasts.length;
    await boardGo(1, { mode });
    return { toasts: __fake.toasts.slice(before).map((t) => t.message).join('\n'), outcome: boardPushOutcome[1], flashed: window.__flashed };
  }, mode);
  // This tab follows another tab's port: it cannot borrow it.
  await page.evaluate(() => { boardConnections[1]._hub.role = 'follower'; });
  for (const mode of ['flash', 'update', 'factory', 'erase']) {
    const r = await attempt(mode);
    expect.soft(r.toasts, mode).toContain('this tab is only following it');
    expect.soft([r.flashed, r.outcome], mode).toEqual([0, { ok: false, aborted: true, reason: 'push did not run' }]);
  }
  // This tab leads, but another tab waits on the hub's Web Lock: borrowing would hand it the port mid-flash.
  await page.evaluate(() => {
    boardConnections[1]._hub.role = 'leader';
    const name = boardConnections[1]._hub.lockName;
    navigator.locks.request(name, () => new Promise((r) => { window.__releaseLock = r; }));
    navigator.locks.request(name, () => {});
  });
  const r = await attempt('flash');
  expect(r.toasts).toContain('is sharing this board');
  expect([r.flashed, r.outcome.reason]).toEqual([0, 'push did not run']);
  expect(await page.locator('#b1-btn-go').isDisabled()).toBe(false);
  await page.evaluate(() => window.__releaseLock?.());
  expect(page.wizErrors).toEqual([]);
});

test('wizard.app_fake_pull_leaves_nothing_pending (should) pulling a board leaves nothing pending in General: no "push to all boards" toast and Push All not flagged (W-19)', async ({ page }) => {
  test.fail(true, 'W-19: syncGeneralFromConfig mirrors the pulled values through onGeneralPasswordChange, onGeneralMacChange and ' +
                  'onGeneralCmdCharChange (app.js:8971-8973), which are the handlers for a user\'s edit: each toasts "Changes pending ' +
                  '— push to all boards to apply" and sets generalSettingsDirty (app.js:1212-1216), so Push All turns amber after ' +
                  'every first pull, with nothing to push');
  await openWizard(page);
  await pullFake(page, 1, board());
  expect(await page.evaluate(() => __fake.toastText())).not.toContain('push to all boards');
  expect(await page.evaluate(() => [generalSettingsDirty, document.getElementById('btn-push-all').classList.contains('btn-pending')]))
    .toEqual([false, false]);
});

test('wizard.app_fake_pending_funcchar (should) a function identifier typed into General but not yet pushed is not used for the board\'s immediate commands - the board still reads its old one (W-13)', async ({ page }) => {
  test.fail(true, 'W-13: onGeneralCmdCharChange writes the typed character into every boardConfigs entry at once (app.js:1332-1339), ' +
                  'and the immediate sends build their command from boardConfigs[n].funcChar: sequence save (app.js:4773) - which then ' +
                  'records the value in the baseline as saved (:4856-4861) - the debug toggles (:9500), the mesh poll (:13423) and more. ' +
                  'The board, still on its old character, broadcasts each line as text to its ports and the mesh (WCB.ino:6063-6066)');
  await openWizard(page);
  await pullFake(page, 1, board(['SEQ,SAVE,hello,;S1hi']), { replies: { '?WDP,DUMP': DUMP } });
  await edit(page, '#g-funcchar', '!');
  expect(await page.evaluate(() => [systemConfig.general.funcChar, boardBaselines[1].funcChar])).toEqual(['!', '?']);
  await page.evaluate(async () => {
    __fake.log.length = 0;
    const row = document.querySelector('#b1-seq-tbody tr');
    const ta = row.querySelector('.seq-val-textarea');
    ta.value = ';S1bye';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    await updateSequence(1, row.id);
    await toggleDebug(1, 'etm');
    __fake.meshOn();
    await meshAutoDiscoverTick();
  });
  const cmds = await page.evaluate(() => __fake.log.filter((e) => e.slot === 1 && e.op !== 'close').map((e) => e.s));
  expect(cmds).toEqual(['?SEQ,SAVE,hello,;S1bye', '?DEBUG,ETM,ON', '?WDP,DUMP']);
});
