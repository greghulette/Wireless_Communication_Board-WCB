// Needs no board: pulls, pushes, managing a board through a relay, and the guards around flashing, erasing and OTA
// (Wizard/app.js, Wizard/flasher.js) - the low-risk gaps the 2026-10-12 coverage re-scan found
// (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each
// test's comment). Fake boards and relays in the real page (lib/fake.js): boardPull, boardGo, boardGoRemote,
// remoteBoardPull and boardGoAll run as on the bench, and only the wire is recorded; flashFirmware runs against a fake
// esptool-js (lib/fake_esptool_rescan.mjs) where the flasher itself is the subject. Runs standalone, in CI, and under
// the harness by id (s30_wizard.py, _FAKE_RESCAN_L). Verify pulls are held back where a spec pushes
// (lib/rescan.js holdVerifyPulls).
//
// Nothing here is a real credential: the mesh password is lib/fake.js's dummy.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../lib/fixtures');
const { board, chain, pullFake, push, edit, relayFake } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince, shown } = require('../lib/rescan');

const sentOn = (page, slot) => page.evaluate((slot) => __fake.sent(slot), slot);
const collects = (page, slot) => page.evaluate((slot) => __fake.log.filter((e) => e.slot === slot && e.op === 'collect').map((e) => e.s), slot);
const sections = (page) => page.evaluate(() => [...document.querySelectorAll('#boards-container [id^="section-board-"]')]
  .map((el) => +el.id.replace('section-board-', '')));
const badge = (page, n) => page.evaluate((n) => document.getElementById(`b${n}-status-badge`).textContent, n);
const pulls = (page, relay) => page.evaluate((relay) => __fake.sent(relay).filter((s) => /MGMT,PULL,\d+,P$/.test(s)), relay);
const FW = '6.2.1_290236RSEP2026';
// A WDP dump a MgmtRelay (WCB19) gives: itself, WCB2 and WCB3 (app_fake.spec.js builds the same from the firmware's
// formats).
const RELAY_DUMP = [
  `[WDP:N=19,CLIENT=0,ALIAS=Relay,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=0,SEEN=1,PEER=3]`,
  `[WDP:N=2,CLIENT=0,ALIAS=Dome,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=3,SEEN=1,PEER=1]`,
  `[WDP:N=3,CLIENT=0,ALIAS=Legs,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=2,SEEN=1,PEER=1]`,
  '[WDPCFG:EN=1,AUTOJOIN=1,PEERS=2]', '[WDP:END,count=3]'].join('\n');

test('wizard.pull_fake_wcbq_renders_floor a pulled WCB quantity above 1 renders that many board sections and nav buttons, a larger one later grows the grid to it, and a board reporting 1 leaves the grid as it is', async ({ page }) => {
  // boardPull's `if (config.wcbQuantity > 1) renderBoards(...)` (app.js:7550), renderBoards / reconcileBoardGrid.
  // Row pull.wcbq_renders_floor.
  await setup(page);
  expect(await sections(page)).toEqual([1]);
  await pullFake(page, 1, board([], { wcbq: 4 }));
  expect(await sections(page)).toEqual([1, 2, 3, 4]);
  expect(await page.evaluate(() => [_boardFloor, document.querySelectorAll('#wcb-nav .wcb-nav-btn').length])).toEqual([4, 4]);
  await pullFake(page, 2, board([], { wcb: 2, wcbq: 6 }));
  expect(await sections(page)).toEqual([1, 2, 3, 4, 5, 6]);
  await pullFake(page, 3, board([], { wcb: 3, wcbq: 1 }));
  expect(await sections(page), 'WCBQ 1 renders nothing new and takes nothing away').toEqual([1, 2, 3, 4, 5, 6]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.pull_fake_success_state a pull shows Pulling... on its button, stores the config and baseline, shows Connected and a toast, asks the version, and gives the button back; a second pull of the same slot while one runs is dropped; a Pull pressed with the board not connected says so, a background one is silent', async ({ page }) => {
  // boardPull (app.js:7437-7646): the in-flight guard, the manual-only toast, the success path. Row pull.success_state.
  await setup(page);
  await pullFake(page, 1, board());
  // Slow the board's backup down so two pulls overlap.
  await page.evaluate(() => {
    const c = boardConnections[1];
    const real = c.sendAndCollect;
    c.sendAndCollect = async (cmd, ...a) => {
      if (cmd === 'WCB_WEBTOOL_CONFIG_PULL') await new Promise((r) => setTimeout(r, 600));
      return real(cmd, ...a);
    };
    boardBaselines[1] = null;
    __fake.log.length = 0;
  });
  const m = await toastMark(page);
  await page.evaluate(() => { window.__p1 = boardPull(1); window.__p2 = boardPull(1); });
  expect(await page.evaluate(() => [document.getElementById('b1-btn-pull').textContent, document.getElementById('b1-btn-pull').disabled,
                                    _boardPullInFlight.has(1)])).toEqual(['Pulling…', true, true]);
  await page.evaluate(async () => { await window.__p1; await window.__p2; });
  await expect.poll(() => collects(page, 1)).toEqual(['WCB_WEBTOOL_CONFIG_PULL', '?version']);
  expect(await page.evaluate(() => [document.getElementById('b1-btn-pull').textContent, document.getElementById('b1-btn-pull').disabled,
                                    _boardPullInFlight.has(1), boardBaselines[1]?.wcbNumber, boardConfigs[1].wcbNumber]))
    .toEqual(['Pull Config', false, false, 1, 1]);
  expect(await badge(page, 1)).toBe('● Connected');
  expect((await toastsSince(page, m)).split('\n').filter((t) => t === 'Config pulled from WCB 1')).toHaveLength(1);

  // Not connected: the Pull button's manual pull says so; a background pull is silent. Neither sends anything.
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  let t = await toastMark(page);
  await page.evaluate(() => boardPull(1));
  expect(await toastsSince(page, t)).toBe('');
  await page.evaluate(() => boardPull(1, { manual: true }));
  expect(await toastsSince(page, t)).toBe('Board not connected');
  expect(await page.evaluate(() => [__fake.log.length, _boardPullInFlight.has(1)])).toEqual([0, false]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.pull_fake_remote_delegation Pull on a relayed board asks its relay (?MGMT,PULL); a slot recorded as its own relay is refused with a toast and nothing sent; Retry puts the badge back to Remote and pulls again through the relay, and on a board no longer remote says so', async ({ page }) => {
  // boardPull's delegation (app.js:7439), boardPullRemote and boardRetryPull (9210-9226). Row pull.remote_delegation.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await page.evaluate(() => { __fake.log.length = 0; });
  // Its own relay: refused.
  await page.evaluate(() => { remoteRelayForBoard[2] = 2; });
  let m = await toastMark(page);
  await page.locator('#b2-btn-pull').click();
  expect(await toastsSince(page, m)).toBe('WCB2 is the relay — connect directly');
  expect(await sentOn(page, 1)).toEqual([]);
  // Back on WCB1: Pull goes through it.
  await page.evaluate(() => { remoteRelayForBoard[2] = 1; });
  await page.locator('#b2-btn-pull').click();
  await expect.poll(() => pulls(page, 1)).toEqual(['?MGMT,PULL,2,P']);
  // That pull gives up (a reply no retry changes): Pull Failed. Retry: Remote again, and a fresh pull.
  await page.evaluate(() => boardConnections[1]._handleLine('[MGMT:CFGERR,2]TOOBIG,L=50000'));
  expect(await badge(page, 2)).toBe('✕ Pull Failed');
  await page.evaluate(() => boardRetryPull(2));
  expect(await badge(page, 2)).toBe('📡 Remote');
  await expect.poll(() => pulls(page, 1)).toEqual(['?MGMT,PULL,2,P', '?MGMT,PULL,2,P']);
  await page.evaluate(() => boardConnections[1]._handleLine('[MGMT:CFGERR,2]TOOBIG,L=50000'));
  // A board that is no longer remote.
  m = await toastMark(page);
  await page.evaluate(() => boardRetryPull(5));
  expect(await toastsSince(page, m)).toBe('Board is no longer set as remote');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_progress_and_history a push shows its progress on the card ("Pushing... k/n settings") as each setting is answered, hides the bar when done, gives the Push button back, releases the mesh-poll hold and clears the controller switch-back history', async ({ page }) => {
  // boardGo's push loop (app.js:8278-8302), reportPushProgress and setFlashUI (10243-10296), its finally. The mesh-poll
  // hold is also wizard.app_fake_mesh_tick's. Row push.progress_and_history.
  await setup(page);
  await pullFake(page, 1, board());
  await holdVerifyPulls(page);
  await page.evaluate(() => {
    window.__progress = [];
    const real = reportPushProgress;
    window.reportPushProgress = (n, done, total) => {
      real(n, done, total);
      const q = (id) => document.getElementById(id);
      window.__progress.push({ status: q(`b${n}-flash-status`).textContent, pct: q(`b${n}-flash-pct`).textContent,
                               bar: q(`b${n}-flash-bar`).style.width, wrap: q(`b${n}-flash-wrap`).style.display,
                               go: q(`b${n}-btn-go`).textContent, pushing: _pushingBoards.has(n) });
    };
    controllerRoleHistory = { navicore: { boards: {} }, kyber: { boards: {} } };   // as a controller switch leaves it
  });
  await edit(page, '#b1-s1-label', 'HILA');
  await edit(page, '#b1-s2-label', 'HILB');
  const r = await push(page, 1, { skipReboot: true });
  expect(r.sent).toEqual(['1:?LABEL,S1,HILA', '1:?LABEL,S2,HILB']);
  expect(await page.evaluate(() => window.__progress)).toEqual([
    { status: 'Pushing… 1/2 settings (50%)', pct: '50%', bar: '50%', wrap: '', go: 'Pushing…', pushing: true },
    { status: 'Pushing… 2/2 settings (100%)', pct: '100%', bar: '100%', wrap: '', go: 'Pushing…', pushing: true },
  ]);
  expect(await page.evaluate(() => {
    const q = (id) => document.getElementById(id);
    return [q('b1-flash-wrap').style.display, q('b1-btn-go').textContent, q('b1-btn-go').disabled, _pushingBoards.has(1),
            controllerRoleHistory];
  })).toEqual(['none', 'Push Config', false, false, { navicore: null, kyber: null }]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.rpush_fake_guards a push to a relayed board is refused, with a toast and the reason in its outcome, when its relay is not connected, when the board has no config, and when no relay is set; nothing is sent', async ({ page }) => {
  // boardGoRemote's guards (app.js:8582-8605). Row rpush.guards.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await holdVerifyPulls(page);
  await page.evaluate(() => { __fake.log.length = 0; });
  const run = async (prep) => {
    await page.evaluate(prep);
    const m = await toastMark(page);
    const r = await page.evaluate(async () => { await boardGo(2); return boardPushOutcome[2]; });
    return { outcome: r, toasts: await toastsSince(page, m), sent: await sentOn(page, 1),
             go: await page.evaluate(() => [document.getElementById('b2-btn-go').textContent, document.getElementById('b2-btn-go').disabled]) };
  };
  let r = await run(() => { boardConnections[1]._connected = false; });
  expect(r).toEqual({ outcome: { ok: false, aborted: true, reason: 'relay WCB 1 not connected' }, toasts: 'WCB 1 (relay) not connected',
                      sent: [], go: ['Push Config', false] });
  r = await run(() => { boardConnections[1]._connected = true; delete boardConfigs[2]; });
  expect(r).toEqual({ outcome: { ok: false, aborted: true, reason: 'no config for this board' }, toasts: 'No config for this board',
                      sent: [], go: ['Push Config', false] });
  // No relay set: boardGo never delegates then, so boardGoRemote is called as Push All's remote stage calls it.
  const m = await toastMark(page);
  const o = await page.evaluate(async () => {
    delete remoteRelayForBoard[2];
    boardPushOutcome[2] = { ok: false, aborted: true, reason: 'push did not run' };
    await boardGoRemote(2);
    return boardPushOutcome[2];
  });
  expect(o).toEqual({ ok: false, aborted: true, reason: 'no relay set for this board' });
  expect(await toastsSince(page, m)).toBe('No relay board set for this board');
  expect(await sentOn(page, 1)).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.pushall_fake_exclusions Push All with no board connected says "No boards connected" and sends nothing - a board whose port has dropped and a MgmtRelay do not count', async ({ page }) => {
  // boardGoAll's start (app.js:9438-9456). The relay and client exclusions are wizard.app_fake_relay_card's and
  // wizard.app_fake_identity's. Row pushall.exclusions.
  await setup(page);
  let m = await toastMark(page);
  await page.evaluate(() => boardGoAll());
  expect(await toastsSince(page, m)).toBe('No boards connected');
  // A board pulled, then its port lost; and a MgmtRelay on USB.
  await pullFake(page, 1, board());
  await pullFake(page, 5, board(['RELAY,1'], { wcb: 19 }));
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  m = await toastMark(page);
  await page.evaluate(() => boardGoAll());
  expect(await toastsSince(page, m)).toBe('No boards connected');
  expect(await page.evaluate(() => __fake.log.filter((e) => e.op === 'send' || e.op === 'await').length)).toBe(0);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_fake_manage_ui a board managed through a relay gets Pull, Push, Identify, Network Test and Stats enabled (with relay tooltips), Factory Reset and Update FW dimmed but clickable with a tooltip saying why, the Remote badge and a Disconnect button; un-managed, all of it goes back', async ({ page }) => {
  // setRemoteConnected (app.js:6591-6657), clearRemoteConnected (6665-6699). Their flash-mode radio handling finds no
  // radios (the card has no b<n>-mode inputs any more), so there is nothing of it to assert. The RTERM,START x3, the
  // badge and the panes are wizard.app_fake_relay_card's and wizard.app_fake_etm_listener's. Row remote.manage_ui.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  const ui = () => page.evaluate(() => {
    const q = (id) => document.getElementById(`b2-${id}`);
    const b = (id) => ({ enabled: !q(id).disabled, opacity: q(id).style.opacity, cursor: q(id).style.cursor, title: q(id).title });
    return { label: q('status-badge').textContent, connect: [q('btn-connect').textContent, q('btn-connect').classList.contains('btn-danger')],
             pull: b('btn-pull'), go: !q('btn-go').disabled, identify: !q('btn-identify').disabled,
             etm: b('btn-etm-char'), stats: b('btn-stats'), erase: b('btn-erase'), update: b('btn-update-fw') };
  });
  const s = await ui();
  expect(s.label).toBe('📡 Remote');
  expect(s.connect).toEqual(['Disconnect', true]);
  expect([s.pull.enabled, s.go, s.identify]).toEqual([true, true, true]);
  expect(s.etm).toEqual({ enabled: true, opacity: '', cursor: '', title: 'Run ETM characterization via wireless relay (~15 seconds)' });
  expect(s.stats).toEqual({ enabled: true, opacity: '', cursor: '', title: 'View ESP-NOW stats via wireless relay' });
  expect(s.erase).toEqual({ enabled: true, opacity: '0.45', cursor: 'not-allowed', title: 'Connect via USB to factory reset — not available over wireless relay' });
  expect(s.update).toMatchObject({ opacity: '0.45', cursor: 'not-allowed', title: 'Connect via USB to update firmware — not available over wireless relay' });

  await page.evaluate(() => clearRemoteConnected(2));
  const c = await ui();
  expect([c.label, c.connect]).toEqual(['Not Connected', ['Connect', false]]);
  expect([c.pull.enabled, c.go, c.identify, c.etm.enabled, c.stats.enabled, c.erase.enabled]).toEqual([false, false, false, false, false, false]);
  expect(c.erase).toMatchObject({ opacity: '', cursor: '', title: 'Erase all settings and reboot — optionally re-flash firmware' });
  expect(c.update).toMatchObject({ opacity: '', cursor: '', title: 'A newer firmware version is available' });
  expect([c.etm.title, c.stats.title]).toEqual(['', '']);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_fake_manage_one a MgmtRelay card\'s "Manage via relay" arms that one board and pulls it through the relay (the card then says managed); it never takes over a board that is on USB itself', async ({ page }) => {
  // relayManageOne (app.js:545-552) from renderRelayCard's button. Manage all (pull=false) is
  // wizard.app_fake_relay_card's. Row remote.manage_one.
  await setup(page);
  await pullFake(page, 1, board(['RELAY,1'], { wcb: 19, wcbq: 3 }), { replies: { '?WDP,DUMP': RELAY_DUMP } });
  await page.evaluate(() => { __fake.meshOn(); return meshAutoDiscoverTick(); });
  const card = page.locator('#relay-card-19');
  const manage = (n) => card.locator('.cs-row', { hasText: `WCB ${n} (` }).locator('button', { hasText: 'Manage via relay' });
  await page.evaluate(() => { __fake.log.length = 0; });
  await manage(2).click();
  await expect.poll(() => pulls(page, 19)).toEqual(['?MGMT,PULL,2,P']);
  expect(await page.evaluate(() => remoteRelayForBoard[2])).toBe(19);
  expect(await card.locator('.cs-row', { hasText: 'WCB 2 (' }).textContent()).toContain('managed');

  // WCB3 comes up on its own USB port: its button does nothing.
  await pullFake(page, 3, board([], { wcb: 3, wcbq: 3 }));
  await page.evaluate(() => { __fake.log.length = 0; });
  await manage(3).click();
  await page.waitForTimeout(400);
  expect(await pulls(page, 19)).toEqual([]);
  expect(await page.evaluate(() => [remoteRelayForBoard[3] ?? null, boardConnections[3].isConnected()])).toEqual([null, true]);
  expect(await badge(page, 3), 'still the USB board it was').toBe('● Connected');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_fake_relay_only_refusals on a board managed through a relay, Update FW, Factory Reset and OTA-over-USB are refused with a toast saying to connect over USB, and nothing is flashed, opened or sent', async ({ page }) => {
  // boardUpdateFW (app.js:1711-1717), boardFactoryReset (10334-10345), boardOtaSerial's guard (1786-1789).
  // Row remote.relay_only_refusals.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await page.evaluate(() => {
    window.__goCalls = 0;
    const real = boardGo;
    window.boardGo = (...a) => { window.__goCalls++; return real(...a); };
    __fake.log.length = 0;
  });
  for (const [what, call, toast] of [
    ['Update FW', () => document.getElementById('b2-btn-update-fw').click(), '🔌 Connect via USB to update firmware — not available over wireless relay'],
    ['Factory Reset', () => document.getElementById('b2-btn-erase').click(), '🔌 Connect via USB to factory reset — not available over wireless relay'],
    ['OTA over USB', () => boardOtaSerial(2), 'OTA-over-serial needs a direct USB connection'],
  ]) {
    const m = await toastMark(page);
    await page.evaluate(`(${call.toString()})()`);
    expect.soft(await toastsSince(page, m), what).toBe(toast);
  }
  expect(await page.evaluate(() => [window.__goCalls, document.getElementById('factory-reset-modal').classList.contains('open'),
                                    _factoryResetSlot, _otaInProgress.size])).toEqual([0, false, null, 0]);
  expect(await sentOn(page, 1)).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.rpull_fake_badges a remote pull shows Retrying while it retries and, when every attempt has failed, Pull Failed with the Retry button; Retry hides it again and a pull that lands shows Remote', async ({ page }) => {
  // _pullAttemptFailed (app.js:9016-9040), updateBoardStatusBadge's Retry button (3650-3654), boardRetryPull.
  // Row rpull.badges.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await page.evaluate(() => { __fake.log.length = 0; });
  const retry = () => shown(page, 'b2-btn-retry');
  const nomem = () => page.evaluate(() => boardConnections[1]._handleLine('[MGMT:CFGERR,2]NOMEM,need 2990 free 2400 largest 1800'));
  await page.evaluate(() => { remoteBoardPull(1, 2); });
  for (let k = 1; k <= 3; k++) {
    await expect.poll(() => pulls(page, 1).then((p) => p.length), { timeout: 8_000 }).toBe(k);
    await nomem();
    if (k < 3) expect([await badge(page, 2), await retry()], `attempt ${k} failed`).toEqual(['↻ Retrying…', false]);
  }
  expect([await badge(page, 2), await retry()]).toEqual(['✕ Pull Failed', true]);
  await page.locator('#b2-btn-retry').click();
  expect([await badge(page, 2), await retry()]).toEqual(['📡 Remote', false]);
  await expect.poll(() => pulls(page, 1).then((p) => p.length)).toBe(4);
  const P = require('../../../Wizard/parser.js');
  const c = chain(board(['LABEL,S1,Back'], { wcb: 2 }));
  await page.evaluate((l) => boardConnections[1]._handleLine(l),
                      `[MGMT:CONFIG,2][VER:${FW}]${c}^?CHK${P.crc32(c).toString(16).toUpperCase().padStart(8, '0')}`);
  await expect.poll(() => page.evaluate(() => boardConfigs[2].serialPorts[0].label)).toBe('Back');
  expect([await badge(page, 2), await retry()]).toEqual(['📡 Remote', false]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flash_fake_erase_no_reconnect the setup wizard\'s erase whose board never comes back says so - "did not come back", reconnect and push by hand - pushes nothing and probes nothing, and gives the card\'s Push button back', async ({ page }) => {
  // boardGo's erase path, its failed reconnect (app.js:8085-8102). The rest of the path is wizard.flash_fake_erase_path's.
  // Row flash.erase_path (its remaining branch).
  await setup(page);
  await pullFake(page, 1, board(['LABEL,S1,Keep']));
  await page.evaluate(() => {
    const c = boardConnections[1];
    updateConnectionUI(1, true);
    c.reconnect = async (...a) => { __fake.log.push({ slot: 1, op: 'reconnect', s: JSON.stringify(a), at: Date.now() }); c._connected = false; return false; };
    _wizardOpen = true;
    __fake.log.length = 0;
  });
  const m = await toastMark(page);
  await page.evaluate(() => boardGo(1, { mode: 'erase' }));
  const log = await page.evaluate(() => __fake.log.filter((e) => e.slot === 1).map((e) => [e.op, e.s]));
  expect(log).toEqual([['send', '?ERASE,NVS'], ['close', ''], ['reconnect', '[10,1500]']]);
  expect(await toastsSince(page, m)).toContain('WCB 1 did not come back — reconnect manually');
  expect(await page.evaluate(() => document.getElementById('term-pane-output-1').textContent))
    .toContain('Could not auto-reconnect — reconnect manually, then push config');
  expect(await page.evaluate(() => [document.getElementById('b1-btn-go').textContent, document.getElementById('b1-btn-go').disabled,
                                    _pushingBoards.has(1), boardFlashMode[1], !!boardBaselines[1]]))
    .toEqual(['Push Config', false, false, 'configure', true]);
  await page.evaluate(() => { _wizardOpen = false; });
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flasher_fake_windows_baud the flasher talks to the ROM at 115200 and loads at 460800 - except on Windows, where it stays at 115200 throughout', async ({ page }) => {
  // flashFirmware's ESPLoader options (flasher.js:351-363), _isWindows (app.js:12-18). Row flasher.windows_baud.
  const FAKE_ESPTOOL = fs.readFileSync(path.join(__dirname, '..', 'lib', 'fake_esptool_rescan.mjs'), 'utf8');
  const NAME = (k) => `WCB_9.9.9_010101RJAN2030_${k}.bin`;
  const FILES = ['ESP32', 'ESP32_part', 'ESP32_boot'];
  await page.route('**/vendor/esptool-js/**', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: FAKE_ESPTOOL }));
  await page.route('https://api.github.com/**', (route) => route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify(FILES.map((k) => ({ type: 'file', name: NAME(k), download_url: `https://fake.invalid/${NAME(k)}` }))) }));
  await page.route('https://fake.invalid/**', (route) => route.fulfill({ status: 200, contentType: 'application/octet-stream',
    body: Buffer.alloc(32, 0x5A) }));
  const loader = () => page.evaluate(async () => {
    globalThis.__wcbEsp = { chip: 'ESP32-D0WD-V3', flash: {}, calls: [] };
    try { await flashFirmware({ fake: true }, 1, { onProgress() {}, onLog() {}, onStatus() {}, appOnly: false }); } catch (_) {}
    return [_isWindows, globalThis.__wcbEsp.calls.filter((c) => c.op === 'loader').map((c) => [c.baudrate, c.romBaudrate])];
  });
  await setup(page);
  expect(await loader()).toEqual([false, [[460800, 115200]]]);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgentData', { configurable: true, get: () => ({ platform: 'Windows' }) });
  });
  await setup(page);
  expect(await loader()).toEqual([true, [[115200, 115200]]]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.ota_fake_entry_guards the OTA button takes the wireless path for a board managed through a relay and the USB path for one on USB; the USB path refuses a board that is not connected, and the wireless path a relay that is not', async ({ page }) => {
  // boardOta (app.js:1738-1741), boardOtaSerial's guards (1786-1789), boardOtaRelay's (2039-2042). The transfers are
  // wizard.ota_fake_serial_stream's and wizard.ota_fake_relay's. Row ota.entry_guards.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await pullFake(page, 3, board([], { wcb: 3, wcbq: 3 }));
  await page.evaluate(() => {
    window.__ota = [];
    window.boardOtaRelay = async (n) => { window.__ota.push(['relay', n]); };
    window.boardOtaSerial = async (n) => { window.__ota.push(['serial', n]); };
  });
  await page.locator('#b2-btn-ota-serial').click();
  await page.locator('#b3-btn-ota-serial').click();
  expect(await page.evaluate(() => window.__ota)).toEqual([['relay', 2], ['serial', 3]]);

  // The real guards: a USB board not connected; a relay not connected.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await pullFake(page, 3, board([], { wcb: 3, wcbq: 3 }));
  await page.evaluate(() => { boardConnections[3]._connected = false; __fake.log.length = 0; });
  let m = await toastMark(page);
  await page.evaluate(() => boardOta(3));
  expect(await toastsSince(page, m)).toBe('Connect the board via USB first');
  await page.evaluate(() => { boardConnections[1]._connected = false; });
  m = await toastMark(page);
  await page.evaluate(() => boardOta(2));
  expect(await toastsSince(page, m)).toBe('Relay WCB 1 not connected');
  expect(await page.evaluate(() => [__fake.log.filter((e) => e.op === 'send' || e.op === 'await').length, _otaInProgress.size]))
    .toEqual([0, 0]);
  expect(page.wizErrors).toEqual([]);
});
