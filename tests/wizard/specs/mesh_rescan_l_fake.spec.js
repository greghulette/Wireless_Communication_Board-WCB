// Needs no board: the stats modal, the WDP mesh panel and its client cards, the RC Controllers panel and its Open
// launcher (Wizard/app.js) - the low-risk gaps the 2026-10-12 coverage re-scan found (docs/hil_plan/RESCAN_2026-10-12.md,
// rows in docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each test's comment). Fake boards and relays
// in the real page (lib/fake.js), their lines fed through the real _handleLine; only the wire is recorded. Runs
// standalone, in CI, and under the harness by id (s30_wizard.py, _FAKE_RESCAN_L).
//
// The WDP dump lines are built from the firmware's printf formats, as specs/app_fake.spec.js's are. Nothing here is a
// real credential.
const { test, expect } = require('../lib/fixtures');
const { board, chain, pullFake, relayFake } = require('../lib/fake');
const { setup, toastMark, toastsSince } = require('../lib/rescan');

const FW = '6.2.1_290236RSEP2026';
const node = (n, { client = 0, alias = '', hw = 24, hwrev = '', fw = FW, cap = '0000', ctrl = 0, tags = '', maestro = '-', age = 1, seen = 1, peer = 1 } = {}) =>
  `[WDP:N=${n},CLIENT=${client},ALIAS=${alias},HW=${hw},HWREV=${hwrev},FW=${fw},CAP=${cap},CTRL=${ctrl},CAPTAGS=${tags},MAESTRO=${maestro},AGE=${age},SEEN=${seen},PEER=${peer}]`;
const dump = (rows, cfg = '[WDPCFG:EN=1,AUTOJOIN=1,PEERS=2]') =>
  [...rows, cfg, `[WDP:END,count=${rows.filter((r) => r.startsWith('[WDP:N=')).length}]`].join('\n');
const output = (page) => page.evaluate(() => document.getElementById('stats-modal-output').textContent);
// The RC cards on show: none while the panel is hidden (an emptied panel is hidden with its last cards left inside).
const rcCards = (page) => page.evaluate(() => (document.getElementById('rc-devices-section').style.display === 'none' ? []
  : [...document.querySelectorAll('#rc-devices-list .rc-device-card')].map((c) => c.textContent.replace(/\s+/g, ' ').trim())));

test('wizard.stats_fake_modal the ETM Network Test shows only the last "ETM Network Characterization" block of the board\'s output; a relayed stats capture that times out shows the lines it did get (or the timeout when it got none); Refresh fetches again; a failed capture shows its error; the mesh poll is held only while a capture runs', async ({ page }) => {
  // openStatsModal, refreshStatsModal, fetchStatsData (app.js:13072-13338). Both capture paths, the pull-reply
  // exclusion and the busy hold are wizard.remote_pull_fake_display's and wizard.app_fake_mesh_tick's. Row stats.modal.
  await setup(page);
  const ETM = ['---------- ETM Network Characterization ----------', 'Phase 1: ping each peer', '[CHAR DEBUG] seq 4',
               'Phase 3: load test', '---------- ETM Network Characterization ----------', 'WCB2: 14 ms avg, 0 lost',
               'Recommended timeout: 250 ms', 'Based on worst peer WCB2'].join('\n');   // as sendAndCollect joins lines
  await pullFake(page, 1, board(), { replies: { '?ETM,CHAR': ETM } });
  await page.evaluate(() => openStatsModal(1, 'etm'));
  await expect.poll(() => output(page)).toBe(['---------- ETM Network Characterization ----------', 'WCB2: 14 ms avg, 0 lost',
                                             'Recommended timeout: 250 ms', 'Based on worst peer WCB2'].join('\n'));
  expect(await page.evaluate(() => [document.getElementById('stats-modal-title').textContent, _statsFetchBusy]))
    .toEqual(['ETM Characterization — WCB 1', false]);
  // Without a banner: the noise lines are dropped and the rest shown.
  await page.evaluate(() => { boardConnections[1].__replies['?ETM,CHAR'] = 'Phase 1\n[CHAR DEBUG] x\n[ETM] tick\nWCB2: 9 ms'; });
  await page.locator('#stats-modal button', { hasText: 'Refresh' }).click();
  await expect.poll(() => output(page)).toBe('Phase 1\nWCB2: 9 ms');
  // A capture that fails: its error.
  await page.evaluate(() => { boardConnections[1].__replies['?ETM,CHAR'] = () => { throw new Error('hil port gone'); }; });
  await page.evaluate(() => refreshStatsModal());
  await expect.poll(() => output(page)).toBe('Error: hil port gone');
  expect(await page.evaluate(() => _statsFetchBusy)).toBe(false);
  await page.evaluate(() => closeStatsModal());

  // Through a relay, with the reply cut short: what arrived is shown once the (shortened) 10 s window closes.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  const open = () => page.evaluate(() => {
    const realSetTimeout = window.setTimeout;
    window.setTimeout = (fn, ms, ...a) => realSetTimeout(fn, ms === 10000 ? 400 : ms, ...a);
    try { openStatsModal(2, 'stats'); } finally { window.setTimeout = realSetTimeout; }
  });
  await open();
  expect(await page.evaluate(() => [_statsFetchBusy, __fake.sent(1).at(-1)])).toEqual([true, '?MGMT,STATS,2']);
  await page.evaluate(() => ['[MGMT:STATS,2]Packets sent: 12', 'Packets lost: 1'].forEach((l) => boardConnections[1]._handleLine(l)));
  await expect.poll(() => output(page)).toBe('Packets sent: 12\nPackets lost: 1');
  expect(await page.evaluate(() => _statsFetchBusy)).toBe(false);
  await open();
  await expect.poll(() => output(page)).toBe('Error: Timed out waiting for stats response from WCB 2');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wdp_fake_render the mesh table marks the querying board\'s own row "this board" (no peer, age or Forget), names capabilities from their bits, calls a NaviCore client a Controller, shows the live-peer count and Poll in the toolbar (Clear learned only with a learned peer), and says when nothing has been discovered', async ({ page }) => {
  // renderWdpMesh (app.js:13640-13740), _wdpCapLabels, _wdpKind, _wdpPlatform, _wdpPeerCell (13610-13638). Row count,
  // quiet device, PWM edge, learned labels and the disabled message are wizard.app_fake_wdp_panel's. Row wdp.render.
  await setup(page);
  const body = () => page.evaluate(() => document.getElementById('wdp-mesh-body').textContent.replace(/\s+/g, ' '));
  // Nothing discovered: the toolbar still shows, with the note.
  await page.evaluate(() => renderWdpMesh([], 1, { enabled: true, autojoin: true, peers: 0 }));
  let t = await body();
  expect(t).toContain("WCB 1 hasn't discovered any neighbors yet");
  expect(t).toContain('Live peers: 0');
  expect(await page.evaluate(() => [...document.querySelectorAll('#wdp-mesh-body .wdp-toolbar button')].map((b) => b.textContent.trim())))
    .toEqual(['ON — heard boards join automatically', 'Poll mesh']);
  // No WDPCFG from the firmware: no toolbar at all.
  await page.evaluate(() => renderWdpMesh([], 3, null));
  expect(await page.evaluate(() => document.querySelector('#wdp-mesh-body .wdp-toolbar'))).toBe(null);

  const parsed = await page.evaluate((d) => parseWdpDump(d), dump([
    node(1, { alias: 'Body', hw: 31, cap: '0109', peer: 3, age: 0 }),
    node(2, { alias: 'Dome', cap: '00F6', peer: 2, age: 4 }),
    node(20, { client: 1, alias: 'NaviCore', hw: 0, tags: 'rc nav', peer: 0, age: 2 }),
    node(15, { client: 1, alias: 'HILProbe', hw: 0, tags: 'hil', peer: 4, age: 3, seen: 0 }),
  ], '[WDPCFG:EN=1,AUTOJOIN=0,PEERS=3]'));
  await page.evaluate((p) => renderWdpMesh(p.nodes, 1, p.cfg), parsed);
  const rows = await page.evaluate(() => [...document.querySelectorAll('#wdp-mesh-body tbody tr')].map((r) => ({
    cls: r.className.trim(), cells: [...r.cells].map((c) => c.textContent.replace(/\s+/g, ' ').trim()),
  })));
  const [self, dome, navi, probe] = rows;
  expect(self.cls).toBe('wdp-self');
  expect(self.cells).toEqual(['1', 'Body this board', 'WCB', 'ESP32-S3', FW, 'HCR, Kyber, DFPlayer', '—', '—', '—', '—', '● this board']);
  expect(dome.cells).toEqual(['2', 'Dome', 'WCB', 'ESP32', FW, 'MP3, WLED, Maestro-remote, PWM, Controller, Maestro-host', '—', '—',
                              'auto-joined✕', '4s', '● live']);
  expect(navi.cells.slice(0, 6)).toEqual(['20', 'NaviCore', 'Controller', 'client', FW, 'rc nav']);
  expect(probe.cls).toBe('wdp-stale');
  expect(probe.cells.slice(2, 3).concat(probe.cells.slice(8))).toEqual(['Client', 'temporary✕', '3s', 'stale']);
  t = await body();
  expect(t).toContain('Live peers: 3');
  expect(t).toContain('OFF — peer list is pinned');
  expect(t).toContain('The mesh as seen from WCB 1 · 4 boards, incl. this board and 3 it hears.');
  expect(await page.evaluate(() => [...document.querySelectorAll('#wdp-mesh-body .wdp-toolbar button')].map((b) => b.textContent.trim())))
    .toEqual(['OFF — peer list is pinned', 'Poll mesh', 'Clear learned']);
  // Only this board so far.
  const alone = await page.evaluate((d) => parseWdpDump(d), dump([node(1, { alias: 'Body', peer: 3 })]));
  await page.evaluate((p) => renderWdpMesh(p.nodes, 1, p.cfg), alone);
  expect(await body()).toContain('The mesh as seen from WCB 1 · only this board so far — no other boards heard yet.');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wdp_fake_refresh_no_board the mesh panel\'s Refresh with no board connected says to connect one; a dump that fails shows the error; the button is given back either way', async ({ page }) => {
  // wdpMeshRefresh (app.js:13763-13781), _wdpMeshConn. Row wdp.refresh_no_board.
  await setup(page);
  await page.evaluate(() => __fake.meshOn());          // the page's own _wdpMeshConn, not lib/fake.js's stand-in
  const body = () => page.evaluate(() => document.getElementById('wdp-mesh-body').textContent.trim());
  await page.locator('#wdp-mesh-refresh').click();
  expect(await body()).toBe('No board connected — connect one first, then Refresh.');
  await pullFake(page, 1, board());
  await page.evaluate(() => { boardConnections[1].__replies['?WDP,DUMP'] = () => { throw new Error('hil: the dump broke'); }; });
  await page.locator('#wdp-mesh-refresh').click();
  await expect.poll(body).toBe('Failed to read the mesh: hil: the dump broke');
  expect(await page.evaluate(() => [document.getElementById('wdp-mesh-refresh').disabled, document.getElementById('wdp-mesh-refresh').textContent]))
    .toEqual([false, '↻ Refresh']);
  // Disconnected again: back to the hint.
  await page.evaluate(() => { boardConnections[1]._connected = false; });
  await page.locator('#wdp-mesh-refresh').click();
  expect(await body()).toBe('No board connected — connect one first, then Refresh.');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.wdp_fake_client_cards a client heard on the mesh gets a card showing its status with how long ago it was heard, its kind (Controller for a NaviCore or SAB alias), the controller it reports to, its firmware and revision and one chip per advertised capability; one advertising rc also appears in the RC Controllers panel', async ({ page }) => {
  // upsertClientCard and renderClientStatus (app.js:13800-13870) from meshAutoDiscoverTick. Online/Offline and the alias
  // seed are wizard.app_fake_mesh_tick's. Row wdp.client_cards.
  await setup(page);
  const DUMP = dump([node(1, { alias: 'Body', peer: 3 }),
                     node(15, { client: 1, alias: 'HILProbe', hw: 0, hwrev: 'revA', fw: '1.0', tags: 'hil rc', ctrl: 20, age: 2 }),
                     node(16, { client: 1, alias: 'SAB Dome', hw: 0, fw: '2.1', tags: '', age: 5 })]);
  await pullFake(page, 1, board([], { wcbq: 2 }), { replies: { '?WDP,DUMP': DUMP } });
  await page.evaluate(() => { __fake.meshOn(); return meshAutoDiscoverTick(); });
  const card = (n) => page.evaluate((n) => {
    const host = document.getElementById(`b${n}-client-status`);
    return { rows: [...host.querySelectorAll('.cs-row')].map((r) => [...r.children].map((c) => c.textContent.replace(/\s+/g, ' ').trim()).join(' ')),
             chips: [...host.querySelectorAll('.cs-cap')].map((c) => c.textContent) };
  }, n);
  expect(await card(15)).toEqual({
    rows: ['Status Online · heard 2s ago', 'Type Client → controller 20', 'Firmware 1.0 (revA)', 'Capabilities hil rc'],
    chips: ['hil', 'rc'],
  });
  expect(await card(16)).toEqual({
    rows: ['Status Online · heard 5s ago', 'Type Controller', 'Firmware 2.1', 'Capabilities none advertised'], chips: [],
  });
  // The rc-capable client feeds the RC panel's launcher.
  expect(await page.evaluate(() => document.getElementById('rc-devices-section').style.display)).toBe('');
  const cards = await rcCards(page);
  expect(cards).toHaveLength(1);
  expect(cards[0]).toContain('RC #15');
  expect(cards[0]).toContain('HILProbe');
  expect(cards[0]).toContain('fw 1.0');
  expect(cards[0]).toContain('live telemetry off');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.rc_fake_discovery an rc_hb line on any board\'s stream makes an RC card (model, mode, firmware, uptime, last seen) that rc_trig and rc_mode update, and that is gone 12 s after the last beat; an RC known only from the mesh shows as "live telemetry off" and goes 40 s after its last sweep', async ({ page }) => {
  // _rcDiscoveryHook and _renderRcDevices (app.js:13438-13530), RC_OFFLINE_MS and RC_WDP_STALE_MS. Row rc.discovery.
  await setup(page);
  await pullFake(page, 1, board());
  const feed = (l) => page.evaluate((l) => boardConnections[1]._handleLine(l), l);
  await feed('{"type":"rc_hb","id":3,"fw":"1.0","model":1,"up":42.4}');
  expect(await page.evaluate(() => document.getElementById('rc-devices-section').style.display)).toBe('');
  let c = await rcCards(page);
  expect(c).toHaveLength(1);
  expect(c[0]).toMatch(/^RC #3 X-Lite · mode — fw 1\.0 · up 42s last seen 0s ago Open ↗$/);
  await feed('{"type":"rc_mode","id":3,"mode":2}');
  await feed('{"type":"rc_trig","id":3,"mode":2,"btn":4,"tap":1}');
  c = await rcCards(page);
  expect(c[0]).toContain('X-Lite · mode 2');
  expect(c[0]).toContain('last trig: m2b4t1');
  // A line that is not JSON, or JSON of another type, makes nothing.
  await feed('{"type":"PONG","id":9}');
  await feed('rc_hb id 9');
  expect(await rcCards(page)).toHaveLength(1);

  // 12 s: still there at 11 s since the last beat, gone at 13 s.
  await page.evaluate(() => { _rcOnlineMap.get(3).lastSeenAt = Date.now() - 11_000; _renderRcDevices(); });
  expect(await rcCards(page)).toHaveLength(1);
  await page.evaluate(() => { _rcOnlineMap.get(3).lastSeenAt = Date.now() - 13_000; _renderRcDevices(); });
  expect(await rcCards(page)).toEqual([]);
  expect(await page.evaluate(() => document.getElementById('rc-devices-section').style.display)).toBe('none');

  // Known only from a WDP sweep: shown without telemetry; kept at 39 s, gone at 41 s.
  await page.evaluate((fw) => upsertClientCard({ n: 18, client: true, alias: 'RCTX', fw, hwRev: '', capTags: 'rc.tx', ctrl: 0,
                                                 age: 1, live: true, peer: 1 }), '3.3');
  c = await rcCards(page);
  expect(c).toHaveLength(1);
  expect(c[0]).toContain('RC #18');
  expect(c[0]).toContain('on the mesh · live telemetry off');
  await page.evaluate(() => { _rcWdpMap.get(18).lastSeenAt = Date.now() - 39_000; _renderRcDevices(); });
  expect(await rcCards(page)).toHaveLength(1);
  await page.evaluate(() => { _rcWdpMap.get(18).lastSeenAt = Date.now() - 41_000; _renderRcDevices(); });
  expect(await rcCards(page)).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.rc_fake_tool_url the RC panel\'s Open link points at the saved config-tool URL (the hosted NaviCore tool by default); "edit Config Tool URL" asks for one and saves it, and an empty answer or the default removes the saved one; Cancel changes nothing', async ({ page }) => {
  // _rcToolUrl, RC_TOOL_URL_KEY and the edit-link click handler (app.js:13372-13380, 14000-14024). Row rc.tool_url.
  await setup(page);
  await pullFake(page, 1, board());
  await page.evaluate(() => boardConnections[1]._handleLine('{"type":"rc_hb","id":3,"fw":"1.0","model":0}'));
  const DEFAULT = 'https://greghulette.github.io/NaviCore/config_tool/';
  const state = () => page.evaluate(() => [localStorage.getItem('rc_config_tool_url'),
                                           document.querySelector('#rc-devices-list .rc-open-btn').getAttribute('href')]);
  expect(await state()).toEqual([null, DEFAULT]);
  // The RC card (and the panel with the link) stays up only 12 s after the last beat: kept fresh here.
  const answer = (a) => page.evaluate((a) => {
    window.__asked = [];
    window.prompt = (msg, cur) => { window.__asked.push(cur); return a; };
    _rcOnlineMap.get(3).lastSeenAt = Date.now();
    _renderRcDevices();
  }, a);
  await answer('  http://127.0.0.1:8779/NaviCore/config_tool/  ');
  await page.locator('#rc-tool-url-edit').click();
  expect(await page.evaluate(() => window.__asked)).toEqual([DEFAULT]);
  expect(await state()).toEqual(['http://127.0.0.1:8779/NaviCore/config_tool/', 'http://127.0.0.1:8779/NaviCore/config_tool/']);
  await answer(null);                                  // Cancel
  await page.locator('#rc-tool-url-edit').click();
  expect(await page.evaluate(() => window.__asked)).toEqual(['http://127.0.0.1:8779/NaviCore/config_tool/']);
  expect((await state())[0]).toBe('http://127.0.0.1:8779/NaviCore/config_tool/');
  await answer(DEFAULT);
  await page.locator('#rc-tool-url-edit').click();
  expect(await state()).toEqual([null, DEFAULT]);
  await answer('http://example.invalid/tool/');
  await page.locator('#rc-tool-url-edit').click();
  await answer('');
  await page.locator('#rc-tool-url-edit').click();
  expect(await state()).toEqual([null, DEFAULT]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.hub_fake_rc_open_tool Open in Config Tool hands the tool ?share=1 when this tab leads the shared port; with a board on direct USB it shares that board first (no reopen) and then sends the tool there with ?share=1 - unless another tab leads the share, when it opens the plain URL; with nothing connected it opens the plain URL and says how sharing works', async ({ page }) => {
  // rcOpenConfigTool (app.js:13382-13436). Row hub.rc_open_tool.
  await setup(page);
  await pullFake(page, 1, board());
  await page.evaluate(() => boardConnections[1]._handleLine('{"type":"rc_hb","id":3,"fw":"1.0","model":0}'));
  await page.evaluate(() => localStorage.setItem('rc_config_tool_url', 'http://127.0.0.1:8779/tool/?x=1'));
  await page.evaluate(() => {
    window.__opened = [];
    window.open = (url, target, features) => {
      const w = { url, target, features: features ?? null, location: {} };
      Object.defineProperty(w.location, 'href', { set(v) { w.navigated = v; }, get() { return w.navigated; } });
      window.__opened.push(w);
      return w;
    };
    window.__realGetSharedHub = getSharedHub;
    window.__shares = 0;
  });
  const click = async () => {
    await page.evaluate(() => { window.__opened.length = 0; _rcOnlineMap.get(3).lastSeenAt = Date.now(); _renderRcDevices(); });
    await page.locator('#rc-devices-list .rc-open-btn').click();
    await page.waitForTimeout(300);
    return page.evaluate(() => window.__opened.map((w) => ({ url: w.url, features: w.features, navigated: w.navigated ?? null })));
  };
  const SHARE = 'http://127.0.0.1:8779/tool/?x=1&share=1';
  const PLAIN = 'http://127.0.0.1:8779/tool/?x=1';

  // This tab leads the shared port: straight to ?share=1.
  await page.evaluate(() => { window.getSharedHub = () => ({ role: 'leader', _portOpen: true }); });
  expect(await click()).toEqual([{ url: SHARE, features: 'noopener', navigated: null }]);

  // A board on direct USB: a blank tab now (inside the click), the board shared in place, then the tab sent to ?share=1.
  await page.evaluate(() => {
    let hub = null;
    window.getSharedHub = () => hub;
    const c = boardConnections[1];
    c.port = { hil: 'P' };
    c.becomeShared = async () => { window.__shares++; hub = { role: 'leader', _portOpen: true }; c._shared = true; };
  });
  let m = await toastMark(page);
  expect(await click()).toEqual([{ url: 'about:blank', features: null, navigated: SHARE }]);
  expect(await page.evaluate(() => window.__shares)).toBe(1);
  expect(await toastsSince(page, m)).toContain('Sharing WCB 1 so the config tool can attach');
  // Shared, but another tab won the port: the plain URL, and a warning.
  await page.evaluate(() => {
    const c = boardConnections[1];
    c._shared = false;
    c.becomeShared = async () => { window.__shares++; window.getSharedHub = () => ({ role: 'follower', _portOpen: false }); c._shared = true; };
    window.getSharedHub = () => null;
  });
  m = await toastMark(page);
  expect(await click()).toEqual([{ url: 'about:blank', features: null, navigated: PLAIN }]);
  expect(await toastsSince(page, m)).toContain('the tool may need a manual connect');
  // Another tab already leads a share: this board is not promoted; the plain URL.
  await page.evaluate(() => { boardConnections[1]._shared = false; window.getSharedHub = () => null; window._anotherTabLeadsShare = async () => true; window.__shares = 0; });
  m = await toastMark(page);
  expect(await click()).toEqual([{ url: 'about:blank', features: null, navigated: PLAIN }]);
  expect(await page.evaluate(() => window.__shares)).toBe(0);
  expect(await toastsSince(page, m)).toContain('Another tab already holds the shared port');

  // Nothing connected: the plain URL, and how sharing works.
  await page.evaluate(() => { boardConnections[1]._connected = false; });
  m = await toastMark(page);
  expect(await click()).toEqual([{ url: PLAIN, features: 'noopener', navigated: null }]);
  expect(await toastsSince(page, m)).toContain('Connect a WCB here first');
  expect(page.wizErrors).toEqual([]);
});
